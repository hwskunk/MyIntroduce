"""
文档元数据存储 — SQLite

追踪逻辑文档与 Milvus 中 chunk 的映射关系。
每篇文档对应多条 Milvus chunk 记录，通过 parent_doc_id 关联。
"""
import sqlite3

import config


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DOCSTORE_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            full_content TEXT NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 1,
            char_count INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            knowledge_path TEXT DEFAULT NULL,
            content_hash TEXT DEFAULT NULL,
            file_size INTEGER DEFAULT NULL,
            file_mtime REAL DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 兼容旧表：新列如果不存在则添加
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    for col_name in ("knowledge_path", "content_hash", "file_size", "file_mtime"):
        if col_name not in cols:
            sql_type = "REAL" if col_name == "file_mtime" else ("INTEGER" if col_name == "file_size" else "TEXT")
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col_name} {sql_type} DEFAULT NULL")
    conn.commit()


# ═══════════════════════════════════════════════════════════════
# CRUD
# ═══════════════════════════════════════════════════════════════

def add_document(
    doc_id: str,
    title: str,
    full_content: str,
    chunk_count: int,
    source: str = "manual",
    knowledge_path: str = "",
    content_hash: str = "",
    file_size: int | None = None,
    file_mtime: float | None = None,
):
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        "INSERT INTO documents (doc_id, title, full_content, chunk_count, char_count, source, knowledge_path, content_hash, file_size, file_mtime) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, title, full_content, chunk_count, len(full_content),
         source, knowledge_path or None, content_hash or None,
         file_size, file_mtime),
    )
    conn.commit()
    conn.close()


def update_document_meta(doc_id: str, file_size: int, file_mtime: float):
    """补记文件元数据（旧记录无 mtime/size 时，同步时用于快速跳过未变化文件）。"""
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        "UPDATE documents SET file_size = ?, file_mtime = ? WHERE doc_id = ?",
        (file_size, file_mtime, doc_id),
    )
    conn.commit()
    conn.close()


def delete_document(doc_id: str):
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    conn.commit()
    conn.close()


def get_document(doc_id: str) -> dict | None:
    conn = _get_conn()
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["doc_id"],
        "title": row["title"],
        "content": row["full_content"],
        "chunk_count": row["chunk_count"],
        "char_count": row["char_count"],
        "source": row["source"],
        "knowledge_path": row["knowledge_path"],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
    }


def get_document_by_hash(content_hash: str) -> dict | None:
    """按 SHA256 内容哈希查找已有文档（用于去重）。"""
    if not content_hash:
        return None
    conn = _get_conn()
    _ensure_table(conn)
    row = conn.execute(
        "SELECT doc_id, title, char_count, source, knowledge_path FROM documents WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["doc_id"],
        "title": row["title"],
        "char_count": row["char_count"],
        "source": row["source"],
        "knowledge_path": row["knowledge_path"],
    }


def list_documents() -> list[dict]:
    """列出所有文档（摘要视图，供 UI 展示）。"""
    conn = _get_conn()
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT doc_id, title, chunk_count, char_count, source, created_at "
        "FROM documents ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["doc_id"],
            "title": r["title"],
            "chunk_count": r["chunk_count"],
            "char_count": r["char_count"],
            "source": r["source"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_documents_detail() -> list[dict]:
    """列出所有文档（含 knowledge_path / content_hash，供增量同步对比）。"""
    conn = _get_conn()
    _ensure_table(conn)
    rows = conn.execute("SELECT * FROM documents").fetchall()
    conn.close()
    return [
        {
            "id": r["doc_id"],
            "title": r["title"],
            "content": r["full_content"],
            "chunk_count": r["chunk_count"],
            "char_count": r["char_count"],
            "source": r["source"],
            "knowledge_path": r["knowledge_path"],
            "content_hash": r["content_hash"],
            "file_size": r["file_size"],
            "file_mtime": r["file_mtime"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_stats() -> dict:
    conn = _get_conn()
    _ensure_table(conn)
    row = conn.execute(
        "SELECT COUNT(*) as total, COALESCE(SUM(char_count), 0) as total_chars FROM documents"
    ).fetchone()
    conn.close()
    return {"total_documents": row["total"], "total_characters": row["total_chars"]}
