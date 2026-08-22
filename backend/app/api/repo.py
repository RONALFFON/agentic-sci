"""仓库信息查询 —— 基于仓库地址识别论文元数据的辅助 API."""

from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class RelationRepository:
    db_path: str

    def fetch_relation_rows(
        self, *, focus_id: int | None, paper_ids: set[int | None], limit: int,
        relation_types: set[str] | None = None, min_score: float = 0.0,
    ) -> list[tuple[int, int, str, float, str]]:
        import sqlite3
        if int(limit) <= 0:
            return []
        if relation_types is not None and not relation_types:
            return []
        rows: list[tuple[int, int, str, float, str]] = []
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            relation_sql = ""
            relation_params: list[object] = []
            if relation_types is not None:
                placeholders = ",".join("?" for _ in relation_types)
                relation_sql = f" AND relation IN ({placeholders})"
                relation_params.extend(sorted(relation_types))
            score_sql = " AND score >= ?" if float(min_score) > 0 else ""
            score_params: list[object] = [float(min_score)] if float(min_score) > 0 else []
            if focus_id is not None:
                cur.execute(
                    """SELECT source_paper_id, target_paper_id, relation, score, evidence
                    FROM paper_relations
                    WHERE (source_paper_id = ? OR target_paper_id = ?)"""
                    + relation_sql + score_sql
                    + " ORDER BY score DESC, updated_at DESC LIMIT ?",
                    (int(focus_id), int(focus_id), *relation_params, *score_params, int(limit)),
                )
            else:
                ids = sorted(int(x) for x in (paper_ids or set()) if int(x) > 0)
                if not ids:
                    return []
                cur.execute("CREATE TEMP TABLE IF NOT EXISTS _kg_pid (id INTEGER PRIMARY KEY)")
                cur.execute("DELETE FROM _kg_pid")
                cur.executemany("INSERT OR IGNORE INTO _kg_pid(id) VALUES (?)", [(i,) for i in ids])
                cur.execute(
                    """SELECT pr.source_paper_id, pr.target_paper_id, pr.relation, pr.score, pr.evidence
                    FROM paper_relations pr
                    INNER JOIN _kg_pid a ON a.id = pr.source_paper_id
                    INNER JOIN _kg_pid b ON b.id = pr.target_paper_id
                    WHERE 1 = 1"""
                    + relation_sql.replace("relation", "pr.relation")
                    + score_sql.replace("score", "pr.score")
                    + " ORDER BY pr.score DESC, pr.updated_at DESC LIMIT ?",
                    (*relation_params, *score_params, int(limit)),
                )
            for sid, tid, rel, score, evidence in cur.fetchall():
                rows.append((int(sid), int(tid), str(rel or ""), float(score or 0.0), str(evidence or "")))
        return rows

    def relation_exists(self, *, source_paper_id: int, target_paper_id: int, relation: str) -> bool:
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT 1 FROM paper_relations
                WHERE source_paper_id = ? AND target_paper_id = ? AND relation = ? LIMIT 1""",
                (int(source_paper_id), int(target_paper_id), str(relation).strip()),
            ).fetchone()
        return row is not None

    def papers_minimal_by_ids(self, paper_ids: set[int]) -> dict[int, tuple[str, int | None, str | None]]:
        import sqlite3
        ids = sorted(int(x) for x in paper_ids if int(x) > 0)
        if not ids:
            return {}
        out: dict[int, tuple[str, int | None, str | None]] = {}
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS _kg_meta (id INTEGER PRIMARY KEY)")
            cur.execute("DELETE FROM _kg_meta")
            cur.executemany("INSERT OR IGNORE INTO _kg_meta(id) VALUES (?)", [(i,) for i in ids])
            cur.execute(
                """SELECT p.id, p.title, p.year, p.category
                FROM papers p INNER JOIN _kg_meta t ON t.id = p.id"""
            )
            for rid, title, year, cat in cur.fetchall():
                out[int(rid)] = (str(title or ""), int(year) if year is not None else None, str(cat) if cat else None)
        return out
