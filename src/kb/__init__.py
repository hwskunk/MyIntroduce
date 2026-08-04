"""
知识库子系统（kb）

对外唯一门面：`knowledge` 模块。
禁止从本包外部直接 import vector_store / doc_store / document_loader / text_splitter。
"""
from src.kb import knowledge  # noqa: F401
