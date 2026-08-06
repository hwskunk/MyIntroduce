"""
MY_INTRO_KB — FastAPI 服务（终端骇客风前端）

职责：REST API + SSE 流式聊天 + 静态前端服务。
复用 src/ 下的 RAG 后端（kb / memory / llm / rag_chain），无 Streamlit。

权限模型：入口填公司名 → 访客；填 ADMIN_CODE → 管理员。
身份传递统一用 `company` 查询参数（HTTP 头不能携带中文，故不用 header）。
管理员接口会校验 company 是否为管理员密钥。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import config
from src import memory
from src.kb import knowledge
from src.rag_chain import get_chain, _contacts_payload

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="MY_INTRO_KB :: SECURE TERMINAL", version="2.0")


# ═══════════════════════════════════════════════════════════════
# 启动预热：后台构建 RAG 链（含知识库同步/Milvus 初始化），
# 让首条聊天消息无需等待重依赖加载
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def _warmup_rag_chain():
    import threading

    def _warm():
        try:
            get_chain()
            print("[Warmup] RAG 链已预热，首条消息可直接流式返回")
        except Exception as e:
            print(f"[Warmup] 预热失败（将在首次请求时重试）: {type(e).__name__}: {e}")

    threading.Thread(target=_warm, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════

def _is_admin(company: str) -> bool:
    return company.strip().upper() == config.ADMIN_CODE.strip().upper()


def _require_admin(company: str) -> str:
    """校验查询参数 company 是否为管理员密钥，否则 403。"""
    company = (company or "").strip()
    if not _is_admin(company):
        raise HTTPException(403, "无管理员权限")
    return company


def _to_lc_message(m: dict):
    from langchain_core.messages import HumanMessage, AIMessage  # 延迟加载
    if m["role"] == "user":
        return HumanMessage(content=m["content"])
    return AIMessage(content=m["content"])


# ═══════════════════════════════════════════════════════════════
# 会话入口
# ═══════════════════════════════════════════════════════════════

@app.post("/api/session")
def create_session(body: dict):
    company = (body.get("company") or "").strip()
    if not company:
        raise HTTPException(400, "公司名称不能为空")
    return {"company": company, "role": "admin" if _is_admin(company) else "visitor"}


@app.get("/api/config")
def get_config():
    """前端配置：本人姓名等（用于 AI 身份标签显示）。"""
    return {"owner_name": config.YOUR_NAME}


@app.get("/api/stats")
def get_stats():
    """知识库统计（landing boot 自检显示真实数据）。"""
    return knowledge.get_stats()


@app.get("/api/contacts")
def get_contacts():
    """联系方式 + 二维码（供前端在历史消息中重新渲染联系区块）。"""
    return _contacts_payload()


@app.get("/api/resume")
def get_resume(download: bool = False):
    """简历文件（访客索要简历时，由前端链接查看/下载）。"""
    path = config.RESUME_FILE
    if not path.exists():
        raise HTTPException(404, "简历文件不存在")
    return FileResponse(
        str(path), media_type="application/pdf",
        filename=f"{config.YOUR_NAME}-简历.pdf",
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/api/thread/history")
def thread_history(company: str = Query(..., description="公司名")):
    company = (company or "").strip()
    if not company:
        raise HTTPException(400, "缺少公司名")
    if _is_admin(company):
        return {"messages": [], "total": 0}
    recent, total = memory.get_recent_messages(company)
    return {"messages": recent, "total": total}


# ═══════════════════════════════════════════════════════════════
# 聊天 — SSE 流式
# ═══════════════════════════════════════════════════════════════

@app.post("/api/chat")
async def chat(body: dict):
    company = (body.get("company") or "").strip()
    message = (body.get("message") or "").strip()
    if not company:
        raise HTTPException(400, "company 不能为空")
    if not message:
        raise HTTPException(400, "message 不能为空")

    thread = company
    memory.add_message(thread, "user", message)

    # 首次调用会构建 RAG 链（含重依赖加载），放线程池避免阻塞事件循环
    chain = await asyncio.to_thread(get_chain)
    recent, _ = memory.get_recent_messages(thread)
    history = [_to_lc_message(m) for m in recent[:-1][-20:]]
    summary = memory.get_summary(thread)

    async def event_gen():
        full = []
        sources = []
        show_contacts = False
        show_resume = False
        try:
            async for evt in chain.stream_answer(message, history, summary):
                t = evt.get("type")
                if t == "delta":
                    full.append(evt["text"])
                    yield {"data": json.dumps({"delta": evt["text"]}, ensure_ascii=False)}
                elif t == "contacts":
                    show_contacts = True
                    yield {"data": json.dumps({"contacts": evt["contacts"]}, ensure_ascii=False)}
                elif t == "resume":
                    show_resume = True
                    yield {"data": json.dumps({"resume": evt["resume"]}, ensure_ascii=False)}
                elif t == "done":
                    sources = evt.get("sources") or []

            # 记账：附带联系方式/简历的消息打标记，刷新后前端可重新渲染区块
            answer = "".join(full)
            meta = None
            if show_contacts:
                meta = {"contacts": True}
            if show_resume:
                meta = dict(meta or {})
                meta["resume"] = True
            memory.add_message(thread, "assistant", answer, meta=meta)
            memory.maybe_summarize(thread)
            yield {"data": json.dumps({"done": True, "sources": sources}, ensure_ascii=False)}
        except Exception as e:
            print(f"[chat] 生成失败: {type(e).__name__}: {e}")
            yield {"data": json.dumps({"error": str(e)}, ensure_ascii=False)}

    # sep="\n"：SSE 事件用 \n\n 分隔（默认 \r\n 会被前端解析漏掉）
    return EventSourceResponse(event_gen(), sep="\n")


# ═══════════════════════════════════════════════════════════════
# 管理员：会话
# ═══════════════════════════════════════════════════════════════

@app.get("/api/admin/threads")
def admin_threads(company: str = Query(...)):
    _require_admin(company)
    return memory.list_threads()


@app.get("/api/admin/thread/{thread_id}")
def admin_thread(thread_id: str, company: str = Query(...)):
    _require_admin(company)
    return memory.get_thread_messages(thread_id)


@app.delete("/api/admin/thread/{thread_id}")
def admin_thread_delete(thread_id: str, company: str = Query(...)):
    _require_admin(company)
    memory.clear_history(thread_id)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# 管理员：上传文件
# ═══════════════════════════════════════════════════════════════

@app.post("/api/admin/upload")
async def admin_upload(company: str = Query(...), files: list[UploadFile] = File(...)):
    _require_admin(company)
    results = []
    for f in files:
        data = await f.read()
        results.append(knowledge.add_uploaded_file(f.filename or "upload", data))
    return {"results": results}


# ═══════════════════════════════════════════════════════════════
# 管理员：知识库文档
# ═══════════════════════════════════════════════════════════════

@app.get("/api/admin/docs")
def admin_docs(company: str = Query(...)):
    _require_admin(company)
    return knowledge.list_documents()


@app.get("/api/admin/docs/{doc_id}")
def admin_doc(doc_id: str, company: str = Query(...)):
    _require_admin(company)
    doc = knowledge.get_document_content(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc


@app.post("/api/admin/rescan")
def admin_rescan(company: str = Query(...)):
    _require_admin(company)
    return knowledge.sync_knowledge_base()


# ═══════════════════════════════════════════════════════════════
# 静态前端
# ═══════════════════════════════════════════════════════════════

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
