"""
RAG 流水线 — 组装层（无 Streamlit 依赖）

只依赖三个门面：src.kb.knowledge / src.memory / src.llm。
通过 RAGChain 单例复用，支持同步 answer 与 SSE 流式 stream_answer。

注意：本模块不再依赖 streamlit，进程内单例由 get_chain() 管理。
"""
import asyncio
import os
import threading
from typing import AsyncIterator

from langchain_core.messages import SystemMessage, HumanMessage

import config
from src.kb import knowledge
from src.llm import get_smart_llm, get_llm


CONTACT_FILE = config.CONTACT_FILE   # 联系方式清单（唯一来源，独立于知识库）

CONTACT_TOOL_NAME = "provide_contact_info"   # 知识库外问题触发的工具名
CONTACT_TOOL = {
    "type": "function",
    "function": {
        "name": CONTACT_TOOL_NAME,
        "description": "当来访者的问题无法由知识库资料回答（知识库暂未收录该内容）时调用。调用后系统会向来访者展示全部联系方式。"
                       "注意：来访者只是在了解项目功能、个人信息或自我介绍（即使你介绍的内容提到'联系方式'、'联系'等字样）时，"
                       "不要调用本工具——那是正常介绍，不是无法回答。只有来访者的问题确实无法由知识库回答时才调用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

RESUME_TOOL_NAME = "provide_resume"   # 访客索要简历文件时触发的工具名
RESUME_TOOL = {
    "type": "function",
    "function": {
        "name": RESUME_TOOL_NAME,
        "description": "当来访者明确索要、查看或下载简历文件时调用（例如：发一份简历给我看看、可以看一下你的简历吗、简历发我一下）。"
                       "调用后系统会自动向来访者展示简历文件的查看/下载按钮，仅用文字回复无法完成简历交付，必须调用本工具。"
                       "注意1：来访者只是询问简历中的具体内容（学历、技能、项目等）时，请直接从知识库回答，不要调用本工具。"
                       "注意2：来访者只是在了解项目功能（如询问 mint.lu 是什么、有哪些功能、是否上线等）时，"
                       "你在介绍中复述'访客索要简历时会自动发送简历文件'这类知识库原文，只是介绍功能，"
                       "不是来访者在向你要简历，绝不要调用本工具。只有来访者明确向你要你自己的简历文件时才调用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# 用户侧防误触发：明确询问简历"内容"（而非索要文件）时，即使模型误调工具也不交付简历
CONTENT_QUERY_MARKERS = (
    "里写了什么", "写了什么", "里面有什么", "包含什么", "有什么内容",
    "有哪些内容", "内容包括", "内容是什么", "写了哪些", "里面都写了",
)
REQUEST_MARKERS = ("发", "给", "看", "要", "寄", "发送", "提供", "发来")


# 索要简历的文本兜底短语：抓"用户索要"与"模型交付"两类语义。
# 不含裸"简历"，避免"简历里有什么"这类内容询问误触发。
RESUME_MARKERS = (
    # 用户索要（模型常把"索要"当纯文字回复，需兜底）
    "发一份简历", "发份简历", "发简历", "简历发", "简历给我",
    "看下简历", "看一下简历", "看看简历", "简历看看", "看看你的简历",
    "看下你的简历", "简历发我", "给我一份简历", "给我看看简历",
    "把简历", "简历给您",
    # 模型交付语（模型回复"这就为您发送简历文件"等却没调工具）
    # 注意：不含裸"简历文件"——模型介绍 mint.lu 功能时必提（知识库原文），会系统性误触发
    "发送简历", "为您发送", "这是我的简历",
)


def _load_contacts() -> list[tuple[str, str]]:
    """读取 connect.md，解析为 (标签, 值) 列表。

    每行一条，形如 "微信：13510562321" / "QQ:1561399437"。
    解析失败或文件不存在时返回空列表。
    """
    try:
        text = CONTACT_FILE.read_text(encoding="utf-8")
    except Exception:
        return []
    contacts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label = value = ""
        for sep in ("：", ":"):
            if sep in line:
                label, _, value = line.partition(sep)
                break
        label, value = label.strip(), value.strip()
        if not label or not value:
            continue
        # 展示名规范化（gmail → Gmail 等）
        label = {"gmail": "Gmail", "outlook": "Outlook", "qq": "QQ"}.get(label.lower(), label)
        contacts.append((label, value))
    return contacts


# 二维码图片（放在 static/images/ 下，改文件即生效）
QR_IMAGES = [
    {"label": "QQ", "url": "/static/images/qq-qr.png"},
    {"label": "微信", "url": "/static/images/wechat-qr.png"},
]


def _contacts_payload() -> dict:
    """联系方式 payload（供 SSE 独立事件下发，前端渲染成独立区块）。

    items: 文字联系方式（来自 connect.md）
    images: 二维码图片（来自 static/images/）
    """
    return {
        "items": [{"label": label, "value": value} for label, value in _load_contacts()],
        "images": QR_IMAGES,
    }


def _resume_payload() -> dict:
    """简历交付 payload（供 SSE 独立事件下发，前端渲染成查看/下载区块）。"""
    return {
        "view_url": "/api/resume",
        "download_url": "/api/resume?download=1",
    }


def _build_system_prompt(context: str) -> str:
    """个人介绍助理系统提示（含检索上下文）。"""
    return f"""你是 {config.YOUR_NAME} 的个人介绍助理。你的职责是帮助来访者（如HR、面试官、合作伙伴）全面了解 {config.YOUR_NAME}。

你掌握的资料来自 {config.YOUR_NAME} 的知识库，包括：
- 个人简历：教育背景、工作经历、技能栈
- 项目经验：做过的项目、担任的角色、技术细节
- 个人特点：性格、工作风格、职业规划
- 其他补充材料

回答原则：
1. **专业真诚**：用专业的语气如实介绍，不夸大不捏造。知识库中没有的信息，绝不要编造。
2. **结构化呈现**：涉及技能、项目等多项内容时，用清晰的结构（如分点、分类）呈现，便于HR快速获取信息。
3. **突出亮点**：当资料中有亮眼的项目经历或技能时，自然地加以强调。
4. **引用来源**：回答末尾用文字标注引用了哪些资料文件，例如"参考：个人简历.md, 项目经验.md"
5. **对话友好**：保持温暖得体的语气，专业自然，不使用任何 emoji 表情符号。
6. **针对性回答**：根据HR的问题精准回答。如果对方问项目经验，就不要罗列教育经历；如果问技能，就聚焦技能。
7. **知识库外的问题（必须调用工具）**：当来访者的问题在知识库资料中没有相关内容（无法回答）时，或来访者直接询问联系方式时，你必须：
   ① 先用一句自然友好的话回应，这句话中**必须包含"知识库暂未收录"或"暂未收录"**（例如："关于这个问题，我的知识库暂未收录相关信息，暂时无法准确回答"）
   ② 然后**必须**调用工具 {CONTACT_TOOL_NAME}（这是硬性要求，不调用工具任务就无法完成）
   系统会自动展示全部联系方式，你**不要**自己罗列。能正常回答且不涉及联系方式的问题，绝不调用该工具。
   **区分（重要）**：如果你已经能正常回答来访者的问题，只是知识库缺少个别细节（如项目网址、部署位置、具体数字），请正常回答并如实说明该细节缺失（如"项目网址暂未公布"），**绝不要**说"暂未收录""未收录""未明确说明""未提供相关信息"等词——这些表述只留给完全无法回答的问题，否则系统会误以为你无法回答而展示联系方式。
8. **简历文件 vs 简历内容（重要区分）**：
   ① 来访者只是询问简历中的具体内容（如"你的简历里写了什么""你的学历/技能/项目是什么"）时，请像回答其他问题一样**直接、详细地回答**，可以自由罗列教育背景、技能、项目等信息，**绝对不要**调用工具，也**不要**说"不能复述"之类的话。
   ② 只有当来访者**明确索要简历文件本身**（如"发一份简历给我""看看你的简历""简历发我一下""把你的简历发给我"）时，你必须调用工具 {RESUME_TOOL_NAME}（这是硬性要求，仅用文字回复简历文件无法展示给对方）。调用后系统会自动展示简历文件按钮，你只需简单回应一句即可。
9. **项目编号不得混淆（重要）**：提及项目时，项目编号、名称与功能描述必须与知识库资料**逐字一致**，例如"混合检索 RAG 智能记忆助手"是项目二（2026.2-2026.4），"Multi-Agent 协同工单自动化平台"是项目三，"零门槛个人知识库智能问答平台（销售智能体）"是项目八，"基于 RAG 的个人介绍智能问答助手 mint.lu"是项目九，"多智能体实习生带教管理平台"是项目十。若你对某个项目的编号或功能对应关系不确定，**直接省略编号只描述功能，绝不要**自行推断或编造编号。
10. **项目网址不得编造（重要）**：项目网址以知识库原文为准——知识库明确写了网址的项目（如项目八、项目十）才能引用对应网址；知识库未提供网址的项目（如项目九），不要编造网址，访客问起时如实说明"网址暂未公布"即可。同样地，项目是否上线以知识库描述为准，知识库写明"已上线"才说已上线，**绝不要**自行推断部署状态或编造访问地址。

如果来访者的问题比较宽泛（如"介绍一下你自己"），请给出一个全面的概述，涵盖教育、技能、项目、职业方向等方面。

【参考资料】：
{context}
"""


def _format_context(docs: list[dict]) -> str:
    """把检索结果组装成参考上下文。"""
    if not docs:
        return "（知识库中暂无相关内容）"
    parts = []
    for i, doc in enumerate(docs):
        parts.append(
            f"【参考资料 {i+1}】来源: {doc.get('source', '未知')}\n"
            f"标题: {doc.get('title', '无标题')}\n"
            f"内容: {doc['content']}\n"
        )
    return "\n".join(parts)


def _collect_sources(docs: list[dict]) -> list[str]:
    """按原始文件名去重，收集引用来源的完整路径。"""
    sources = []
    seen = set()
    for d in docs:
        fname = d.get("source_file", "") or d.get("source", "")
        if not fname or fname in seen:
            continue
        seen.add(fname)
        sources.append(os.path.join(config.DATA_DIR, fname))
    return sources


def _safe_extract(response) -> str:
    """安全提取 LLM 输出文本。"""
    if hasattr(response, "content"):
        return str(response.content)
    if isinstance(response, dict):
        return str(response.get("content", response.get("output", "")))
    return str(response)


class RAGChain:
    """RAG 流水线（进程内单例）。

    每次请求：混合检索 → 组装消息（System→总结→最近10轮→用户输入）→ LLM 生成。
    """

    def __init__(self):
        # 首次构建时保证知识库与 data/ 一致（增量同步）
        knowledge.sync_knowledge_base()
        self.llm = get_smart_llm()
        # 工具调用用低温实例："必须调用工具"的指令更易被稳定遵循
        self.llm_tool = get_llm(temperature=0.1)

    def prepare(self, input_text: str, chat_history: list | None, summary: str | None) -> tuple[list, list[str]]:
        """检索 + 组装消息。返回 (messages, sources)。"""
        docs = knowledge.search(input_text, top_k=config.TOP_K)
        sources = _collect_sources(docs)

        messages = [SystemMessage(content=_build_system_prompt(_format_context(docs)))]
        if summary:
            messages.append(SystemMessage(content=f"【之前的对话总结】\n{summary}"))
        if chat_history:
            messages.extend(chat_history)
        messages.append(HumanMessage(content=input_text))
        return messages, sources

    def answer(self, input_text: str, chat_history: list | None = None, summary: str | None = None) -> tuple[str, list[str]]:
        """同步生成。返回 (text, sources)。"""
        messages, sources = self.prepare(input_text, chat_history, summary)
        response = self.llm.invoke(messages)
        return _safe_extract(response), sources

    async def _stream_not_found_message(self, question: str) -> AsyncIterator[str]:
        """知识库外问题：生成自然的"未收录 + 欢迎联系"语句。

        联系方式不在此生成，由系统通过 contacts 事件独立下发。
        """
        sys_msg = SystemMessage(content=(
            f"你是 {config.YOUR_NAME} 的个人介绍助理。来访者问了一个你的知识库中没有收录的问题。\n"
            "请用自然、友好、得体的语气简短回应，内容包括：\n"
            "1. 说明关于这个问题，目前你的知识库暂未收录相关信息，暂时无法准确回答\n"
            "2. 欢迎对方通过页面下方展示的联系方式联系你进一步交流\n"
            "不要列出具体联系方式（系统会自动展示），不要使用 emoji，控制在 60 字以内。"
        ))
        async for chunk in self.llm.astream([sys_msg, HumanMessage(content=question)]):
            delta = getattr(chunk, "content", None)
            if delta:
                yield delta

    async def _stream_resume_reply(self, question: str) -> AsyncIterator[str]:
        """索要简历：生成一句自然的交付语（简历文件由系统独立下发）。"""
        sys_msg = SystemMessage(content=(
            f"你是 {config.YOUR_NAME} 的个人介绍助理。来访者向你索要了简历文件。\n"
            "请用自然、友好、得体的语气简短回应（30 字以内），表示乐意提供简历，"
            "不要描述简历内容（系统会自动展示简历文件按钮），不要使用 emoji。"
        ))
        async for chunk in self.llm.astream([sys_msg, HumanMessage(content=question)]):
            delta = getattr(chunk, "content", None)
            if delta:
                yield delta

    async def stream_answer(self, input_text: str, chat_history: list | None = None, summary: str | None = None) -> AsyncIterator[dict]:
        """SSE 流式生成（知识库外问题会触发联系方式工具）。

        Yield:
            {"type": "delta", "text": str}                文本增量
            {"type": "contacts", "contacts": [...]}       知识库外：独立联系方式块
            {"type": "resume", "resume": {...}}           索要简历：独立简历文件块
            {"type": "done", "sources": [...]}            结束
        """
        # 检索为阻塞操作（embedding API + BM25），放线程池避免阻塞事件循环
        messages, sources = await asyncio.to_thread(self.prepare, input_text, chat_history, summary)
        tool_triggered = False
        resume_triggered = False
        streamed: list[str] = []

        try:
            llm_with_tools = self.llm_tool.bind_tools([RESUME_TOOL, CONTACT_TOOL])
            async for chunk in llm_with_tools.astream(messages):
                delta = getattr(chunk, "content", None)
                if delta:
                    streamed.append(delta)
                    yield {"type": "delta", "text": delta}
                for tc in getattr(chunk, "tool_call_chunks", []) or []:
                    if tc.get("name") == CONTACT_TOOL_NAME:
                        tool_triggered = True
                    elif tc.get("name") == RESUME_TOOL_NAME:
                        resume_triggered = True
        except Exception as e:
            # 工具调用模式不可用（模型/网关不支持等）→ 回退普通流式
            print(f"[RAG] 工具调用模式失败，回退普通模式: {type(e).__name__}: {e}")
            streamed = []
            async for chunk in self.llm.astream(messages):
                delta = getattr(chunk, "content", None)
                if delta:
                    streamed.append(delta)
                    yield {"type": "delta", "text": delta}

        answer_text = "".join(streamed)
        # 兜底判定：模型调用了工具，或回答文本明确表达了"知识库未收录"
        # （工具调用不可靠——模型有时只输出文字不调工具，需文本兜底保证联系方式一定出现）
        NOT_FOUND_MARKERS = (
            "暂未收录", "未收录", "无法准确回答", "无法回答",
            "未提及", "未提到", "没有相关信息", "暂无相关信息",
            "没有相关", "未找到相关", "未检索到",
            # 模型常用变体（测试 #42 发现：答"资料中未明确说明"漏触发）
            # 保留"未明确说明"的代价：模型如实陈述细节缺失（"部署位置未明确说明"）时
            # 会误弹联系方式（无害）；删除则 #42 类问题漏触发率 80%（实测），业务损失更大
            "未明确说明", "未提供相关信息",
        )
        text_not_found = any(m in answer_text for m in NOT_FOUND_MARKERS)
        # 输入完全没提"简历"时，模型输出中的"发送简历"等只可能是复述项目功能
        # （知识库原文，如"访客索要简历时自动发送简历文件"），不是简历交付语——
        # 要求输入也提到简历，文本兜底才生效。真实索要场景输入必然含"简历"二字，不受影响。
        input_mentions_resume = "简历" in input_text or "resume" in input_text.lower() or "cv" in input_text.lower()
        text_resume = input_mentions_resume and any(m in answer_text for m in RESUME_MARKERS)

        # 索要简历：优先于联系方式处理（简历场景不应触发联系方式）。
        # 用户侧防误触发：询问简历"内容"（非索要文件）时，即使模型误调工具也不交付。
        is_content_query = any(m in input_text for m in CONTENT_QUERY_MARKERS)
        has_request = any(m in input_text for m in REQUEST_MARKERS)
        resume_requested = not (is_content_query and not has_request)
        if (resume_triggered or text_resume) and resume_requested:
            if not answer_text.strip():
                async for delta in self._stream_resume_reply(input_text):
                    yield {"type": "delta", "text": delta}
            yield {"type": "resume", "resume": _resume_payload()}
            yield {"type": "done", "sources": sources}
            return

        if tool_triggered or text_not_found:
            # 知识库外：模型未输出有效语句时才生成自然语句；然后独立下发联系方式
            if not answer_text.strip():
                async for delta in self._stream_not_found_message(input_text):
                    yield {"type": "delta", "text": delta}
            yield {"type": "contacts", "contacts": _contacts_payload()}
            yield {"type": "done", "sources": []}
            return

        if not answer_text.strip():
            yield {"type": "delta", "text": "抱歉，知识库中暂时没有找到相关信息。"}
        yield {"type": "done", "sources": sources}


_chain: RAGChain | None = None
_chain_lock = threading.Lock()


def get_chain() -> RAGChain:
    """获取 RAG 链单例（进程内，首次调用时构建）。

    用锁保护，避免并发首次构建（预热线程与请求线程同时进入）。
    """
    global _chain
    if _chain is None:
        with _chain_lock:
            if _chain is None:
                _chain = RAGChain()
    return _chain
