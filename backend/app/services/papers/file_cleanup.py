"""数据库提交后清理托管文件；失败任务留在数据库供后台重试。"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


def guard_deleted_papers(conn, table):
    """新建派生表也安装保护，阻止删除期间在途请求重新创建孤立记录。"""
    allowed = {'paper_memory', 'paper_reader_turns', 'paper_opening_cache',
               'paper_pdf_excerpt_cache', 'paper_reading_sessions', 'paper_processing_jobs'}
    if table not in allowed:
        raise ValueError('unsupported paper table')
    conn.execute('CREATE TABLE IF NOT EXISTS deleted_papers(paper_id INTEGER PRIMARY KEY)')
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_reject_deleted
        BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM deleted_papers WHERE paper_id=NEW.paper_id)
        BEGIN SELECT RAISE(IGNORE); END""")


def enqueue_cleanup(conn, data_root, paths):
    conn.execute("CREATE TABLE IF NOT EXISTS file_cleanup_queue(path TEXT PRIMARY KEY)")
    root = os.path.realpath(data_root)
    for path in paths:
        path = os.path.realpath(path)
        try:
            inside = os.path.commonpath([root, path]) == root and path != root
        except ValueError:
            inside = False
        if inside:
            conn.execute("INSERT OR IGNORE INTO file_cleanup_queue(path) VALUES(?)", (path,))


def retry_file_cleanup(db_path):
    with sqlite3.connect(db_path, timeout=30) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_cleanup_queue'").fetchone():
            return
        root = os.path.dirname(os.path.abspath(db_path))
        referenced = {os.path.realpath(os.path.join(root, row[0])) for row in
                      conn.execute("SELECT local_pdf_path FROM papers WHERE local_pdf_path IS NOT NULL")}
        for (path,) in conn.execute("SELECT path FROM file_cleanup_queue LIMIT 100").fetchall():
            if path in referenced:
                continue
            try:
                if os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) != os.path.realpath(root):
                    continue
                os.remove(path)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                logger.warning("托管文件清理失败，将重试: %s", path)
                continue
            conn.execute("DELETE FROM file_cleanup_queue WHERE path=?", (path,))
