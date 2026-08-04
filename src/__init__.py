"""
MyIntroduce 核心包

分层结构（依赖单向向下）：
- 入口层: app.py
- 组装层: src.rag_chain
- 子系统: src.kb（知识库） / src.memory（记忆） / src.llm（模型）
- 底层:   config
"""
