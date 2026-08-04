"""
全局配置 — MyIntroduce

纯配置模块，位于依赖链最底层，不 import 任何项目内部模块。
所有路径基于本文件位置解析，确保从任何目录运行都能正确加载。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# ── 路径配置 ──
_BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = _BASE_DIR / "data"                    # 知识库源文件
MILVUS_DIR = _BASE_DIR / "milvus_data"           # Milvus Lite 数据目录
MILVUS_DB_PATH = MILVUS_DIR / "my_intro_kb.db"   # Milvus Lite 单文件库
MEMORY_DB_PATH = _BASE_DIR / "conversations.db"  # 会话记忆 SQLite
DOCSTORE_DB_PATH = _BASE_DIR / "knowledge.db"    # 文档元数据 SQLite
CONTACT_FILE = _BASE_DIR / "connect.md"          # 联系方式清单（独立于知识库，避免污染检索上下文）

# ── 加载 .env（Key 一律从环境变量读取，禁止硬编码）──
ENV_PATH = _BASE_DIR / ".env"
load_dotenv(ENV_PATH)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
if not DASHSCOPE_API_KEY:
    raise RuntimeError(
        "DASHSCOPE_API_KEY 未设置！请在 MyIntroduce/.env 中配置。\n"
        "参考 .env.example 文件，或设置环境变量 DASHSCOPE_API_KEY。\n"
        f"期望的 .env 路径: {ENV_PATH}"
    )

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# ── LLM 模型 ──
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "qwen-plus")
FAST_LLM_MODEL_NAME = os.environ.get("FAST_LLM_MODEL_NAME", "qwen-turbo")

# ── Embedding（DashScope 云端 API，无需本地模型）──
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v3")
DENSE_DIM = 1024

# ── Milvus 向量库 ──
MILVUS_DB_PATH = os.environ.get(
    "MILVUS_DB_PATH", str(MILVUS_DB_PATH)
)
MILVUS_COLLECTION_NAME = os.environ.get(
    "MILVUS_COLLECTION_NAME", "my_intro_knowledge"
)

# ── 切片策略（§2.4）──
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "50"))
LONG_LINE_THRESHOLD = 150  # 单行长句断行阈值

# ── 检索策略（§3，两段式：Dense+BM25 → RRF → top-N）──
RECALL_PER_LEG = int(os.environ.get("RECALL_PER_LEG", "10"))  # 每路粗排召回量
RRF_K = int(os.environ.get("RRF_K", "60"))                     # RRF 融合常数
TOP_K = int(os.environ.get("TOP_K", "5"))                      # 最终返回 top-N

# ── 记忆系统（§6）──
RECENT_WINDOW = 20     # 最近保留的完整消息条数（10 轮）
SUMMARY_INTERVAL = 10  # 每新增 10 条消息触发一次总结

# ── 个人介绍对象 ──
YOUR_NAME = os.environ.get("YOUR_NAME", "你的名字")

# ── 管理员密钥（输入此值进入管理员模式）──
ADMIN_CODE = os.environ.get("ADMIN_CODE", "SKUNK")
