"""
会话记忆管理 — SQLite 持久化 + Summary Buffer（对齐 SellerAgent §6）

- 最近 10 轮（20 条）完整保留在 prompt 中
- ≥20 条消息 → 触发总结检查
- 每新增 10 条 → LLM 蒸馏旧对话 → 一份精炼总结
- Prompt 拼接顺序: system → 对话总结 → 最近10轮 → 用户输入

MyIntroduce 为单会话场景，默认 thread_id="main"；接口支持多线程。
"""
import json
import sqlite3

import config

RECENT_WINDOW = config.RECENT_WINDOW    # 最近保留的完整消息条数（10 轮）
SUMMARY_INTERVAL = config.SUMMARY_INTERVAL  # 每新增 10 条消息触发一次总结
DEFAULT_THREAD = "main"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.MEMORY_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            meta TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS summaries (
            thread_id TEXT PRIMARY KEY,
            summary_text TEXT NOT NULL DEFAULT '',
            summarized_until_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_thread ON messages(thread_id);
        CREATE INDEX IF NOT EXISTS idx_created ON messages(thread_id, created_at);
    """)
    # 兼容旧表：meta 列缺失时补充
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "meta" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT DEFAULT NULL")
    conn.commit()


# ═══════════════════════════════════════════════════════════════
# CRUD
# ═══════════════════════════════════════════════════════════════

def add_message(thread_id: str, role: str, content: str, meta: dict | None = None):
    """追加一条消息到会话历史。

    meta: 可选附加信息，如 {"contacts": True} 标记该消息附带联系方式区块，
          前端刷新后据此重新渲染联系区块。
    """
    conn = _get_conn()
    _ensure_tables(conn)
    conn.execute(
        "INSERT INTO messages (thread_id, role, content, meta) VALUES (?, ?, ?, ?)",
        (thread_id, role, content, json.dumps(meta, ensure_ascii=False) if meta else None),
    )
    conn.commit()
    conn.close()


def get_recent_messages(thread_id: str) -> tuple[list[dict[str, str]], int]:
    """获取最近 RECENT_WINDOW 条完整消息 + 当前总消息数。

    Returns:
        (messages, total_count)
        messages: 最近 20 条完整消息（时间正序）
        total_count: 该线程消息总数
    """
    conn = _get_conn()
    _ensure_tables(conn)
    total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id = ?", (thread_id,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT role, content, meta FROM messages WHERE thread_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (thread_id, RECENT_WINDOW),
    ).fetchall()
    conn.close()
    messages = []
    for r in reversed(rows):
        msg = {"role": r["role"], "content": r["content"]}
        if r["meta"]:
            try:
                msg["meta"] = json.loads(r["meta"])
            except Exception:
                pass
        messages.append(msg)
    return messages, total


def get_summary(thread_id: str) -> str | None:
    """获取指定线程的对话总结。"""
    conn = _get_conn()
    _ensure_tables(conn)
    row = conn.execute(
        "SELECT summary_text FROM summaries WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    conn.close()
    if row and row["summary_text"].strip():
        return row["summary_text"]
    return None


def clear_history(thread_id: str):
    """清除指定会话的所有历史 + 总结。"""
    conn = _get_conn()
    _ensure_tables(conn)
    conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM summaries WHERE thread_id = ?", (thread_id,))
    conn.commit()
    conn.close()


def list_threads() -> list[dict]:
    """列出所有会话及其统计信息（供管理员面板）。

    Returns:
        [{"thread_id", "total_messages", "total_rounds",
          "first_time", "last_time", "has_summary", "title"}, ...]
        按最后活跃时间倒序。
    """
    conn = _get_conn()
    _ensure_tables(conn)
    rows = conn.execute("""
        SELECT m.thread_id,
               COUNT(*) as total,
               MAX(m.created_at) as last_time,
               MIN(m.created_at) as first_time,
               s.summary_text
        FROM messages m
        LEFT JOIN summaries s ON m.thread_id = s.thread_id
        GROUP BY m.thread_id
        ORDER BY last_time DESC
    """).fetchall()

    # 每个会话第一条用户消息作为标题
    first_msgs = {}
    for r in rows:
        first = conn.execute(
            "SELECT content FROM messages WHERE thread_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1",
            (r["thread_id"],),
        ).fetchone()
        first_msgs[r["thread_id"]] = first["content"] if first else ""
    conn.close()

    return [
        {
            "thread_id": r["thread_id"],
            "total_messages": r["total"],
            "total_rounds": (r["total"] + 1) // 2,
            "first_time": r["first_time"],
            "last_time": r["last_time"],
            "has_summary": bool(r["summary_text"] and r["summary_text"].strip()),
            "title": _make_thread_title(first_msgs.get(r["thread_id"], "")),
        }
        for r in rows
    ]


def _make_thread_title(first_message: str) -> str:
    """用第一条用户消息截断生成会话标题。"""
    if first_message:
        clean = first_message.replace("\n", " ").strip()
        return clean[:20] + ("…" if len(clean) > 20 else "")
    return "（无消息）"


def get_thread_messages(thread_id: str) -> list[dict]:
    """获取指定会话的完整消息列表（供管理员查看）。"""
    conn = _get_conn()
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT role, content, meta, created_at FROM messages WHERE thread_id = ? ORDER BY id ASC",
        (thread_id,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        item = {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        if r["meta"]:
            try:
                item["meta"] = json.loads(r["meta"])
            except Exception:
                pass
        result.append(item)
    return result


# ═══════════════════════════════════════════════════════════════
# Summary Buffer
# ═══════════════════════════════════════════════════════════════

def _get_last_summarized_id(conn: sqlite3.Connection, thread_id: str) -> int:
    row = conn.execute(
        "SELECT summarized_until_id FROM summaries WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    return row["summarized_until_id"] if row else 0


def _get_messages_range(
    conn: sqlite3.Connection, thread_id: str, start_id: int, end_id: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE thread_id = ? AND id > ? AND id <= ? ORDER BY id ASC",
        (thread_id, start_id, end_id),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def maybe_summarize(thread_id: str) -> bool:
    """检查是否需要触发总结，如果需要则执行。

    触发条件:
    1. 消息总数 > RECENT_WINDOW（20条/10轮）
    2. 距离上次总结新增了 >= SUMMARY_INTERVAL 条（10条/5轮）

    总结内容: 旧总结 + 新离开窗口的对话 → LLM 重写 → 新的精炼总结

    Returns:
        True 如果执行了总结，False 如果不需要
    """
    # 惰性加载 src.llm（LangChain 重依赖），避免模块 import 时被拖慢
    conn = _get_conn()
    _ensure_tables(conn)

    total = conn.execute(
        "SELECT MAX(id) FROM messages WHERE thread_id = ?", (thread_id,)
    ).fetchone()[0]

    if total is None or total <= RECENT_WINDOW:
        conn.close()
        return False

    last_summarized = _get_last_summarized_id(conn, thread_id)

    # 距离上次总结不够 SUMMARY_INTERVAL 条
    if total - last_summarized < SUMMARY_INTERVAL:
        conn.close()
        return False

    # 加载旧总结
    old_summary = ""
    row = conn.execute(
        "SELECT summary_text FROM summaries WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if row and row["summary_text"].strip():
        old_summary = row["summary_text"].strip()

    # 新离开窗口的消息范围: (last_summarized, total - RECENT_WINDOW]
    new_window_end = total - RECENT_WINDOW
    new_messages = _get_messages_range(conn, thread_id, last_summarized, new_window_end)

    if not new_messages:
        conn.close()
        return False

    conn.close()

    # 调用 LLM 生成新总结
    new_summary = _generate_summary(old_summary, new_messages)

    # 写入 summaries 表
    conn = _get_conn()
    _ensure_tables(conn)
    conn.execute("""
        INSERT INTO summaries (thread_id, summary_text, summarized_until_id, updated_at)
        VALUES (?, ?, ?, datetime('now', 'localtime'))
        ON CONFLICT(thread_id) DO UPDATE SET
            summary_text = excluded.summary_text,
            summarized_until_id = excluded.summarized_until_id,
            updated_at = datetime('now', 'localtime')
    """, (thread_id, new_summary, new_window_end))
    conn.commit()
    conn.close()

    print(f"[Memory] Summary updated for {thread_id}: {len(new_summary)} chars, "
          f"summarized_until_id={new_window_end}")
    return True


def _generate_summary(old_summary: str, new_messages: list[dict[str, str]]) -> str:
    """调用 LLM 将旧总结 + 新对话蒸馏为新的精炼总结。

    用 qwen-plus（文档 §五：总结蒸馏），max_tokens=512。
    """
    from src.llm import get_llm
    from langchain_core.messages import SystemMessage, HumanMessage

    # 格式化新对话
    new_text = ""
    for m in new_messages:
        role_label = "访客" if m["role"] == "user" else "助理"
        new_text += f"{role_label}: {m['content']}\n"

    summary_system = """你是一个对话总结助手。你的任务是把已有的历史总结和新发生的对话合并成一段精炼的结构化总结。

总结格式：
已讨论的话题：（列举主要话题）
访客关注点：（访客最关心这个人的哪些方面，如经历、技能、项目、职业规划）
访客个人信息：（访客说过的自己的信息，如身份、职位、所在公司，没有则写"无"）
未解决的问题：（访客提过但还没回答的事，没有则写"无"）
关键信息：（其他重要的上下文）

要求：
1. 控制在200字以内
2. 保留所有关键信息，不要遗漏
3. 如果旧总结和新对话信息冲突，以新对话为准
4. 用中文"""

    summary_user = f"""已有历史总结：
{old_summary or "（暂无，这是第一次总结）"}

新发生的对话：
{new_text}

请合并为新的结构化总结。"""

    try:
        # 文档 §五: 总结蒸馏用 qwen-plus, max_tokens=512
        llm = get_llm(temperature=0, model=config.LLM_MODEL_NAME, max_tokens=512)
        response = llm.invoke([
            SystemMessage(content=summary_system),
            HumanMessage(content=summary_user),
        ])
        return response.content.strip()
    except Exception as e:
        print(f"[Memory] Summary generation failed: {e}")
        # 降级：返回旧总结
        return old_summary
