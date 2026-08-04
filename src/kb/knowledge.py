"""
知识库编排层 — kb 分区对外唯一门面

职责：把 data/ 源文件 ↔ doc_store ↔ Milvus 串起来做增量同步，并对外提供检索。
外部（rag_chain / app）只允许 import 本模块。

扩展点提示：
- 换向量库：只改 vector_store，本门面接口不变
- 加 Reranker 精排：在 search() 内对结果再排一次，上层无感
"""
import hashlib
import uuid
from pathlib import Path

import config
from src.kb import document_loader, text_splitter, doc_store
from src.kb.vector_store import get_kb


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _insert_document(file_path: Path, content: str) -> dict:
    """切分 + 入库 + 记录元数据（内容已由调用方解析好，避免重复解析）。"""
    kb = get_kb()
    content_hash = _hash_content(content)
    title = file_path.stem
    source = file_path.name

    chunks = text_splitter.split_document(title=title, content=content, source=source)
    doc_id = uuid.uuid4().hex[:12]

    kb.insert_chunks(doc_id=doc_id, title=title, source=source, chunks=chunks)
    doc_store.add_document(
        doc_id=doc_id, title=title, full_content=content,
        chunk_count=len(chunks), source=source,
        knowledge_path=str(file_path), content_hash=content_hash,
    )
    return {"id": doc_id, "title": title, "chunk_count": len(chunks), "char_count": len(content)}


def _delete_document(doc_id: str):
    get_kb().delete_chunks(doc_id)
    doc_store.delete_document(doc_id)


def sync_knowledge_base() -> dict:
    """扫描 data/ 与 doc_store/Milvus 对比，增量同步。

    - 新文件（path 未入库）→ 新增
    - 内容变化（同 path hash 不同）→ 删旧重插
    - 已删除文件（入库但不在 data/）→ 移除
    """
    stats = {"added": [], "changed": [], "removed": [], "unchanged": 0}

    # data/ 当前文件: path_str → Path
    data_files = {str(fp): fp for fp in document_loader.scan_files()}

    # doc_store 已入库: knowledge_path → doc 记录
    indexed = {}
    for doc in doc_store.list_documents_detail():
        kp = doc.get("knowledge_path")
        if kp:
            indexed[kp] = doc

    # 1) 新增 / 内容变化
    for path_str, fp in data_files.items():
        try:
            content = document_loader.parse_file(fp)
        except Exception as e:
            print(f"[Knowledge] 跳过解析失败文件 {fp.name}: {e}")
            continue
        content_hash = _hash_content(content)

        existing = indexed.get(path_str)
        if existing is None:
            _insert_document(fp, content)
            stats["added"].append(fp.name)
            print(f"[Knowledge] 新增文档: {fp.name}")
        elif existing.get("content_hash") != content_hash:
            _delete_document(existing["id"])
            _insert_document(fp, content)
            stats["changed"].append(fp.name)
            print(f"[Knowledge] 内容变化，重新入库: {fp.name}")
        else:
            stats["unchanged"] += 1

    # 2) 已删除文件
    for path_str, doc in indexed.items():
        if path_str not in data_files:
            _delete_document(doc["id"])
            stats["removed"].append(Path(path_str).name)
            print(f"[Knowledge] 移除已删除文档: {Path(path_str).name}")

    print(f"[Knowledge] 同步完成: 新增{len(stats['added'])} "
          f"变化{len(stats['changed'])} 删除{len(stats['removed'])} 未变{stats['unchanged']}")
    return stats


def add_uploaded_file(file_name: str, file_bytes: bytes) -> dict:
    """把上传的文件保存到 data/ 并同步入库（供管理员面板）。

    Args:
        file_name: 原始文件名（会自动做 basename 清洗，防路径穿越）
        file_bytes: 文件内容字节

    Returns:
        sync_knowledge_base() 的统计结果 {"added": [...], "changed": [...], ...}
    """
    safe_name = Path(file_name).name
    dest = config.DATA_DIR / safe_name
    # 覆盖写入：同名文件视为"更新"，sync 会按 content_hash 识别变化并重新入库
    existed = dest.exists()
    dest.write_bytes(file_bytes)
    print(f"[Knowledge] 上传文件已保存: {dest} ({'覆盖' if existed else '新增'})")
    return sync_knowledge_base()


def get_document_content(doc_id: str) -> dict | None:
    """获取单篇文档完整内容（供管理员查看知识库文档）。"""
    return doc_store.get_document(doc_id)


def search(query: str, top_k: int | None = None) -> list[dict]:
    """检索知识库（混合检索 Dense+BM25→RRF），返回最相关的 chunk 列表。"""
    return get_kb().hybrid_search(query, top_k=top_k)


def list_documents() -> list[dict]:
    """列出所有知识文档（供 UI 展示）。"""
    return doc_store.list_documents()


def get_stats() -> dict:
    """知识库统计（文档数/总字符数）。"""
    return doc_store.get_stats()
