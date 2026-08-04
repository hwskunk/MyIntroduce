"""
模型工厂 — MyIntroduce

通过 DashScope 兼容模式调用 Qwen 模型。
只负责对话 LLM；Embedding 客户端由 kb/vector_store 自建，避免 kb 依赖本模块。
"""
from langchain_openai import ChatOpenAI

import config


def get_llm(
    temperature: float = 0.3,
    model: str = config.LLM_MODEL_NAME,
    max_tokens: int = 2048,
) -> ChatOpenAI:
    """获取 ChatOpenAI 实例（通过 DashScope 调用 Qwen 模型）。"""
    return ChatOpenAI(
        model=model,
        base_url=config.DASHSCOPE_BASE_URL,
        api_key=config.DASHSCOPE_API_KEY,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def get_fast_llm() -> ChatOpenAI:
    """快速/廉价 LLM — 用于对话总结等简单任务。"""
    return get_llm(temperature=0, model=config.FAST_LLM_MODEL_NAME, max_tokens=256)


def get_smart_llm() -> ChatOpenAI:
    """智能 LLM — 用于回复生成。"""
    return get_llm(temperature=0.3, model=config.LLM_MODEL_NAME, max_tokens=2048)
