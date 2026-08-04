"""
Milvus 向量库客户端 — 混合检索（DashScope Embedding + BM25 关键词）

存储粒度：文本切片（chunk），通过 parent_doc_id 关联到逻辑文档。
检索管线（两段式，对齐 SellerAgent §3，当前不启用 Rerank）：
  Dense ANN (text-embedding-v3, RECALL_PER_LEG) + BM25 (rank-bm25, RECALL_PER_LEG)
  → RRF 融合去重 (k=RRF_K) → 取 top-K

说明：
- 稠密向量：DashScope text-embedding-v3 (1024d)，API 调用，无需本地模型
- 关键词检索：rank-bm25（独立于 Milvus，纯内存索引，首次检索时构建）
- BM25 分词：jieba 优先，缺省回退字符级 bigram
- 本模块是 kb 分区内部模块，外部请通过 knowledge.py 门面访问
"""
import os
import time
from datetime import datetime

import config

RECALL_PER_LEG = config.RECALL_PER_LEG
RRF_K = config.RRF_K


class MilvusKB:
    """Milvus 知识库客户端。

    pymilvus / langchain_openai 为重量级依赖，延迟到方法内 import，
    避免模块 import 时被拖慢（加快前端首屏）。
    """

    def __init__(self):
        self.client = None                 # MilvusClient
        self._dense_embedding = None       # DashScope OpenAIEmbeddings
        self._bm25 = None                  # rank-bm25 索引
        self._bm25_texts: list[str] = []   # 与 BM25 索引对应的原始文本
        self._bm25_metas: list[dict] = []  # 与 BM25 索引对应的元数据
        self._bm25_dirty = True            # 是否需要重建 BM25 索引

    # ── 初始化 ──

    def init(self):
        from pymilvus import MilvusClient  # 延迟加载重依赖

        os.makedirs(os.path.dirname(config.MILVUS_DB_PATH), exist_ok=True)
        self.client = MilvusClient(str(config.MILVUS_DB_PATH))

        if self.client.has_collection(config.MILVUS_COLLECTION_NAME):
            self.client.load_collection(config.MILVUS_COLLECTION_NAME)
            print(f"[Milvus] Loaded collection: {config.MILVUS_COLLECTION_NAME}")
        else:
            self._create_collection()
        return self

    def list_doc_ids(self) -> set[str]:
        """返回 Milvus 中实际存在的所有 parent_doc_id（用于与 SQLite 同步校验）。"""
        if not self.client or not self.client.has_collection(config.MILVUS_COLLECTION_NAME):
            return set()
        ids = set()
        offset = 0
        while True:
            batch = self.client.query(
                collection_name=config.MILVUS_COLLECTION_NAME,
                filter="id >= 0",
                output_fields=["parent_doc_id"],
                limit=500, offset=offset,
            )
            if not batch:
                break
            for row in batch:
                ids.add(row.get("parent_doc_id", ""))
            offset += len(batch)
        return ids

    def _create_collection(self):
        """创建集合（简单 API，兼容 milvus-lite）。"""
        self.client.create_collection(
            collection_name=config.MILVUS_COLLECTION_NAME,
            dimension=config.DENSE_DIM,
            metric_type="IP",
        )
        self.client.load_collection(config.MILVUS_COLLECTION_NAME)
        print(f"[Milvus] Created collection: {config.MILVUS_COLLECTION_NAME}")

    # ── Embedding（DashScope 云端）──

    def _get_dense_embedding(self):
        """获取 DashScope Embedding 客户端（text-embedding-v3）。"""
        if self._dense_embedding is None:
            from langchain_openai import OpenAIEmbeddings  # 延迟加载重依赖
            import httpx
            http_client = httpx.Client(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
            self._dense_embedding = OpenAIEmbeddings(
                model=config.EMBEDDING_MODEL,
                openai_api_key=config.DASHSCOPE_API_KEY,
                openai_api_base=config.DASHSCOPE_BASE_URL,
                tiktoken_enabled=False,
                check_embedding_ctx_length=False,
                http_client=http_client,
            )
        return self._dense_embedding

    def _encode_dense(self, texts: list[str]) -> list[list[float]]:
        """DashScope API 并行编码（最多 3 并发，每批 ≤10 条）。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        emb = self._get_dense_embedding()
        batches = []
        for i in range(0, len(texts), 10):
            batches.append((i // 10, texts[i:i + 10]))
        if not batches:
            return []

        results = {}
        _t0 = time.time()
        with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
            futures = {
                pool.submit(emb.embed_documents, batch): idx
                for idx, batch in batches
            }
            for f in as_completed(futures):
                results[futures[f]] = f.result()
        _elapsed = time.time() - _t0

        all_vectors = []
        for i in range(len(batches)):
            all_vectors.extend(results[i])

        print(f"[Milvus] Embedding API: {len(texts)} texts in {len(batches)} batches → {_elapsed:.1f}s")
        return all_vectors

    # ── Chunk CRUD ──

    def insert_chunks(
        self,
        doc_id: str,
        title: str,
        source: str,
        chunks: list[dict],
        # chunks: [{"index": 0, "content": "...", "lines": "1-5", "source": "..."}, ...]
    ) -> int:
        """批量插入文档的所有 chunk 到 Milvus。"""
        if not self.client:
            raise RuntimeError("Milvus 未初始化")
        if not chunks:
            return 0

        contents = [c["content"] for c in chunks]
        vectors = self._encode_dense(contents)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 简单 API 的 id 是 int64，用 doc_id 哈希做基数确保唯一且可关联
        base = abs(hash(doc_id)) % (10 ** 12)

        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": base * 10000 + i,
                "vector": vectors[i],
                "parent_doc_id": doc_id,
                "title": title,
                "content": chunk["content"],
                "chunk_index": chunk["index"],
                "chunk_count": len(chunks),
                "chunk_lines": chunk.get("lines", ""),
                "source": source,
                "created_at": created_at,
            })

        self.client.insert(collection_name=config.MILVUS_COLLECTION_NAME, data=data)
        self._bm25_dirty = True  # 数据变更，下次检索时重建 BM25
        return len(chunks)

    def delete_chunks(self, doc_id: str) -> int:
        """删除指定文档的所有 chunk。"""
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        result = self.client.delete(
            collection_name=config.MILVUS_COLLECTION_NAME,
            filter=f'parent_doc_id == "{doc_id}"',
        )
        if isinstance(result, dict):
            deleted = result.get("deleted_count", 0) or result.get("delete_count", 0)
        elif isinstance(result, list):
            deleted = len(result)
        else:
            deleted = 0
        self._bm25_dirty = True
        print(f"[Milvus] Deleted {deleted} chunks for doc: {doc_id}")
        return deleted

    # ── 混合检索 ──

    def _tokenize(self, text: str) -> list[str]:
        """中文分词，用于 BM25 关键词检索。

        优先使用 jieba，不可用时回退到字符级 bigram（BM25 仍有区分度）。
        """
        try:
            import jieba
            return [t for t in jieba.cut(text) if t.strip()]
        except ImportError:
            # 字符级 bigram：对中文友好，对英文退化到空格分词
            import re
            tokens = []
            for word in text.split():
                if re.search(r'[一-鿿]', word):
                    chars = re.findall(r'[一-鿿]', word)
                    for i in range(len(chars)):
                        if i < len(chars) - 1:
                            tokens.append(chars[i] + chars[i+1])
                        else:
                            tokens.append(chars[i])
                else:
                    tokens.append(word.lower())
            return [t for t in tokens if t.strip()]

    def _build_bm25(self):
        """从 Milvus 加载所有 chunk 文本，构建 rank-bm25 索引。"""
        if not self.client:
            return

        from rank_bm25 import BM25Okapi

        # 分页加载所有 chunk
        all_chunks = []
        offset = 0
        while True:
            batch = self.client.query(
                collection_name=config.MILVUS_COLLECTION_NAME,
                filter="id >= 0",
                output_fields=["id", "parent_doc_id", "title", "content",
                               "source", "created_at", "chunk_index",
                               "chunk_count", "chunk_lines"],
                limit=200, offset=offset,
            )
            if not batch:
                break
            all_chunks.extend(batch)
            offset += len(batch)

        self._bm25_texts = [c.get("content", "") for c in all_chunks]
        self._bm25_metas = all_chunks  # 保存完整元数据，检索时直接取用

        tokenized = [self._tokenize(t) for t in self._bm25_texts]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None
        self._bm25_dirty = False
        print(f"[Milvus] BM25 index built: {len(self._bm25_texts)} chunks")

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        """用 rank-bm25 做关键词检索，返回格式与 dense 搜索一致。"""
        if self._bm25_dirty or self._bm25 is None:
            self._build_bm25()

        if not self._bm25 or not self._bm25_texts:
            return []

        tokenized = self._tokenize(query)
        if not tokenized:
            return []

        scores = self._bm25.get_scores(tokenized)
        indexed = list(enumerate(scores))
        indexed.sort(key=lambda x: x[1], reverse=True)
        top_indices = [i for i, s in indexed[:top_k] if s > 0]

        results = []
        for idx in top_indices:
            meta = self._bm25_metas[idx]
            score = float(scores[idx])
            results.append({
                "id": meta.get("id", ""),
                "parent_doc_id": meta.get("parent_doc_id", ""),
                "title": meta.get("title", ""),
                "content": meta.get("content", ""),
                "source": meta.get("source", ""),
                "source_file": meta.get("source", ""),
                "chunk_index": meta.get("chunk_index", 0),
                "chunk_count": meta.get("chunk_count", 1),
                "chunk_lines": meta.get("chunk_lines", ""),
                "score": round(score, 4),
                "_bm25_score": score,
            })
        return results

    def _rrf_fuse(
        self,
        dense_results: list[dict],
        bm25_results: list[dict],
        k: int = RRF_K,
    ) -> list[dict]:
        """RRF (Reciprocal Rank Fusion) 融合稠密和关键词检索结果。"""
        merged: dict[str, dict] = {}
        rrf_scores: dict[str, float] = {}

        for rank, doc in enumerate(dense_results):
            cid = doc.get("id", "")
            if cid not in merged:
                merged[cid] = doc
                rrf_scores[cid] = 0.0
            rrf_scores[cid] += 1.0 / (k + rank + 1)

        for rank, doc in enumerate(bm25_results):
            cid = doc.get("id", "")
            if cid not in merged:
                merged[cid] = doc
                rrf_scores[cid] = 0.0
            rrf_scores[cid] += 1.0 / (k + rank + 1)

        sorted_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)

        results = []
        for cid in sorted_ids:
            doc = merged[cid]
            doc["score"] = round(rrf_scores[cid], 4)
            doc.pop("_bm25_score", None)
            results.append(doc)

        return results

    def hybrid_search(self, query: str, top_k: int | None = None) -> list[dict]:
        """混合检索：Dense (DashScope API) + BM25 → RRF 融合 → top-K。

        两段式管线，不启用 Rerank。如需加 Reranker 精排，可在门面层
        （knowledge.search）对结果再排一次，保持本模块聚焦。
        """
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        top_k = top_k or config.TOP_K
        t_start = time.perf_counter()

        # ── Step 1: Dense 粗排（DashScope embedding + Milvus ANN）──
        t1 = time.perf_counter()
        dense_vec = self._encode_dense([query])[0]
        t_dense_encode = time.perf_counter() - t1

        dense_raw = self.client.search(
            collection_name=config.MILVUS_COLLECTION_NAME,
            data=[dense_vec],
            anns_field="vector",
            limit=RECALL_PER_LEG,
            output_fields=["id", "parent_doc_id", "title", "content",
                           "source", "created_at", "chunk_index",
                           "chunk_count", "chunk_lines"],
            search_params={"metric_type": "IP", "params": {"nprobe": 16}},
        )

        dense_results = []
        for hit in dense_raw[0]:
            entity = hit.get("entity", hit)
            pid = entity.get("parent_doc_id", "")
            if not pid:
                continue
            chunk_idx = entity.get("chunk_index", 0)
            chunk_total = entity.get("chunk_count", 1)
            lines = entity.get("chunk_lines", "")
            source_label = f"{entity.get('title', '')} · 片段{chunk_idx + 1}/{chunk_total}"
            if lines:
                source_label += f" (第{lines}行)"

            dense_results.append({
                "id": entity.get("id", ""),
                "parent_doc_id": pid,
                "title": entity.get("title", ""),
                "content": entity.get("content", ""),
                "source": source_label,
                "source_file": entity.get("source", ""),
                "created_at": entity.get("created_at", ""),
                "chunk_index": chunk_idx,
                "chunk_count": chunk_total,
                "chunk_lines": lines,
                "score": round(hit.get("distance", 0), 4),
            })
        t_dense = time.perf_counter() - t1

        # ── Step 2: BM25 粗排 ──
        t2 = time.perf_counter()
        bm25_results = self._bm25_search(query, RECALL_PER_LEG)
        t_bm25 = time.perf_counter() - t2

        # ── Step 3: RRF 融合去重 → 取 top-K ──
        t3 = time.perf_counter()
        fused = self._rrf_fuse(dense_results, bm25_results, k=RRF_K)
        results = fused[:top_k]
        t_fusion = time.perf_counter() - t3

        t_total = time.perf_counter() - t_start
        print(
            f"[Milvus] search timing: "
            f"encode={t_dense_encode:.2f}s "
            f"dense={t_dense:.2f}s ({len(dense_results)} docs) "
            f"bm25={t_bm25:.2f}s ({len(bm25_results)} docs) "
            f"fusion={t_fusion:.2f}s ({len(fused)} candidates → {len(results)}) "
            f"total={t_total:.2f}s"
        )
        return results


_kb_instance: MilvusKB | None = None


def get_kb() -> MilvusKB:
    """获取 MilvusKB 单例。"""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = MilvusKB()
        _kb_instance.init()
    return _kb_instance
