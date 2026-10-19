"""持久化的论文后处理队列：每进程一个消费者，SQLite 原子领取，失败最多尝试三次。"""
from __future__ import annotations

import logging
import json
import os
import sqlite3
import threading
import time
from uuid import uuid4

logger = logging.getLogger(__name__)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_processing_jobs (
        paper_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'queued', phase TEXT DEFAULT 'queued',
        llm_classify INTEGER NOT NULL, download_pdfs INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_run REAL DEFAULT 0, lease_until REAL DEFAULT 0,
        owner TEXT, rerun INTEGER DEFAULT 0, error TEXT, updated_at REAL NOT NULL)""")
    from .file_cleanup import guard_deleted_papers
    guard_deleted_papers(conn, 'paper_processing_jobs')


def enqueue(conn, paper_id, options):
    conn.execute("""INSERT INTO paper_processing_jobs(paper_id,llm_classify,download_pdfs,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
        llm_classify=MAX(llm_classify,excluded.llm_classify),
        download_pdfs=MAX(download_pdfs,excluded.download_pdfs),
        rerun=CASE WHEN status='running' THEN 1 ELSE 0 END,
        attempts=CASE WHEN status='running' THEN attempts ELSE 0 END,
        status=CASE WHEN status='running' THEN status ELSE 'queued' END,
        next_run=0,error=NULL,updated_at=excluded.updated_at""",
        (paper_id, int(options['llm_classify']), int(options['download_pdfs']), time.time()))


def list_jobs(db_path, paper_ids):
    if not paper_ids:
        return []
    with sqlite3.connect(db_path, timeout=30) as conn:
        ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        placeholders = ','.join('?' for _ in paper_ids)
        return [dict(row) for row in conn.execute(
            f"SELECT paper_id,status,phase,attempts,error FROM paper_processing_jobs WHERE paper_id IN ({placeholders})",
            paper_ids)]


def retry_job(db_path, paper_id):
    with sqlite3.connect(db_path, timeout=30) as conn:
        ensure_schema(conn)
        return conn.execute("""UPDATE paper_processing_jobs SET status='queued',phase='queued',attempts=0,
            next_run=0,error=NULL,updated_at=? WHERE paper_id=? AND status='failed'
            AND EXISTS(SELECT 1 FROM papers WHERE id=paper_id)""", (time.time(), paper_id)).rowcount > 0


def claim_job(db_path):
    now = time.time()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        # A crashed consumer cannot leave a paper permanently running.
        conn.execute("""UPDATE paper_processing_jobs SET status='failed',error='任务中断次数超过限制'
            WHERE status='running' AND lease_until<? AND attempts>=3""", (now,))
        row = conn.execute("""SELECT * FROM paper_processing_jobs WHERE
            (status='queued' AND next_run<=?) OR (status='running' AND lease_until<? AND attempts<3)
            ORDER BY updated_at,paper_id LIMIT 1""", (now, now)).fetchone()
        if row is None:
            return None
        owner = uuid4().hex
        conn.execute("""UPDATE paper_processing_jobs SET status='running',attempts=attempts+1,
            owner=?,lease_until=?,updated_at=? WHERE paper_id=?""", (owner, now+900, now, row['paper_id']))
        return {**dict(row), 'owner': owner, 'attempts': row['attempts']+1}


def _phase(db_path, job, phase):
    with sqlite3.connect(db_path, timeout=30) as conn:
        updated = conn.execute("""UPDATE paper_processing_jobs SET phase=?,updated_at=?
            WHERE paper_id=? AND owner=? AND status='running'
            AND EXISTS(SELECT 1 FROM papers WHERE id=paper_id)""",
            (phase, time.time(), job['paper_id'], job['owner'])).rowcount
    if not updated:
        raise RuntimeError('论文已删除或任务已被接管')


def process_job(db, job):
    from ...core.paper_paths import library_pdf_relative_path
    from ...core.pdf_download import download_paper_pdf_to_path
    from ...settings import get_settings
    from ..graph.kg_relations import build_relations_for_new_paper
    from ..llm.agent_runtime import execution_scope, check_active
    from .papers_library_service import _merge_tag_lists

    pid = job['paper_id']
    errors = []
    # Independent from the HTTP request that persisted the paper.
    with execution_scope(600):
        paper = db.get_paper_by_id(pid)
        if paper is None:
            return
        if job['llm_classify']:
            _phase(db.db_path, job, 'classify')
            try:
                from ...agents import get_paper_analysis_agent
                agent = get_paper_analysis_agent()
                cat, tags = agent.classify_for_library(paper.title, paper.abstract, paper.journal,
                    paper.keywords or [], existing_categories=db.list_library_categories_by_count(limit=80))
                check_active()
                _phase(db.db_path, job, 'classify')
                with db._get_connection() as conn:
                    # Preserve manual edits made while classification was running.
                    conn.execute("""UPDATE papers SET category=?,tags=?,updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND category IS ? AND tags=?""",
                        (cat, json.dumps(_merge_tag_lists(paper.tags or [], tags), ensure_ascii=False),
                         pid, paper.category, json.dumps(paper.tags or [], ensure_ascii=False)))
                    # A PDF retry must not repeat paid classification that already succeeded.
                    conn.execute("""UPDATE paper_processing_jobs SET llm_classify=0
                        WHERE paper_id=? AND owner=? AND rerun=0""", (pid, job['owner']))
            except Exception:
                logger.warning('论文 %s 分类失败', pid, exc_info=True)
                errors.append('分类失败')
        paper = db.get_paper_by_id(pid)
        if paper is None:
            return
        if job['download_pdfs']:
            _phase(db.db_path, job, 'pdf')
            try:
                current = db._abs_local_pdf(paper.local_pdf_path)
                if not current or not os.path.isfile(current):
                    rel = library_pdf_relative_path(paper.category, pid, paper.title)
                    dest = os.path.join(db._data_root(), rel)
                    if not download_paper_pdf_to_path(paper, dest, email=get_settings().ncbi_email or ''):
                        raise RuntimeError('未找到可下载 PDF')
                    # Deletion may race with a download; never leave an unreferenced PDF behind.
                    with db._get_connection() as conn:
                        updated = conn.execute('UPDATE papers SET local_pdf_path=? WHERE id=?', (rel, pid)).rowcount
                        if not updated:
                            from .file_cleanup import enqueue_cleanup
                            enqueue_cleanup(conn, db._data_root(), [dest])
            except Exception:
                logger.warning('论文 %s PDF 下载失败', pid, exc_info=True)
                errors.append('PDF 下载失败或没有可用链接')
        check_active()
        _phase(db.db_path, job, 'graph')
        try:
            # Build after PDF persistence so relation extraction can use full text.
            build_relations_for_new_paper(db.db_path, pid)
        except Exception:
            logger.warning('论文 %s 图谱构建失败', pid, exc_info=True)
            errors.append('图谱构建失败')
        if errors:
            raise RuntimeError('；'.join(errors))


def finish_job(db_path, job, error=None):
    with sqlite3.connect(db_path, timeout=30) as conn:
        status = ('queued' if job['attempts'] < 3 else 'failed') if error else 'completed'
        conn.execute("""UPDATE paper_processing_jobs SET
            status=CASE WHEN rerun=1 THEN 'queued' ELSE ? END,
            attempts=CASE WHEN rerun=1 THEN 0 ELSE attempts END,rerun=0,
            phase=?,error=?,next_run=?,lease_until=0,owner=NULL,updated_at=?
            WHERE paper_id=? AND owner=? AND status='running'""",
            (status, 'failed' if error else 'completed', error, time.time()+15*job['attempts'] if error else 0,
             time.time(), job['paper_id'], job['owner']))


class PaperProcessingWorker:
    def __init__(self, db):
        self.db = db
        self.stop_event = threading.Event()
        self.active_budget = None
        self.thread = threading.Thread(target=self.run, name='paper-processing', daemon=True)
        with sqlite3.connect(db.db_path, timeout=30) as conn:
            ensure_schema(conn)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.active_budget is not None:
            self.active_budget.cancelled.set()
        self.thread.join(timeout=5)

    def run(self):
        from .file_cleanup import retry_file_cleanup
        while not self.stop_event.is_set():
            try:
                retry_file_cleanup(self.db.db_path)
                job = claim_job(self.db.db_path)
                if job:
                    try:
                        from ..llm.agent_runtime import execution_scope
                        with execution_scope(600, independent=True) as budget:
                            self.active_budget = budget
                            if self.stop_event.is_set():
                                budget.cancelled.set()
                            process_job(self.db, job)
                    except Exception as exc:
                        finish_job(self.db.db_path, job, str(exc)[:500])
                    else:
                        finish_job(self.db.db_path, job)
                    finally:
                        self.active_budget = None
                    continue
            except Exception:
                logger.exception('论文后台队列处理失败')
            self.stop_event.wait(2)
