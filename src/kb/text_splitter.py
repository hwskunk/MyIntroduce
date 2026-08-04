"""
五层递归切片 — 对齐 SellerAgent §2.4

chunk_size=500, overlap=50，Markdown 感知 + 表格保护 + 行号追踪。

切分优先级（递归）：
1. 表格 — 整表不拆（先提取出来单独处理）
2. Markdown 标题 (##) — 标题前切开
3. 段落边界 (\\n\\n) → 行边界 (\\n)
4. 句子边界 (。！？) → 子句边界 (；，)
5. 硬切

本模块是纯文本处理，不依赖其他 kb 模块（预处理/表格规范化也在此）。
"""
import re

import config


# ═══════════════════════════════════════════════════════════════
# 预处理（§2.2）
# ═══════════════════════════════════════════════════════════════

def normalize_for_splitting(text: str) -> str:
    """为单行长文本按句子边界插入换行，使行号追踪有意义。

    同时保护 Markdown 特殊区域（表格、代码块）不被拆坏。
    """
    lines = text.split("\n")
    result = []

    in_code_fence = False
    in_table = False

    for line in lines:
        stripped = line.strip()

        # 追踪代码块边界
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            result.append(line)
            continue
        if in_code_fence:
            result.append(line)
            continue

        # 追踪表格边界
        is_table_line = bool(re.match(r'^\|.*\|$', stripped))
        is_separator_line = bool(re.match(r'^[\|\s\-:]+$', stripped))
        if is_table_line and not in_table:
            in_table = True
        elif not is_table_line:
            in_table = False
        if in_table:
            result.append(line)
            continue

        # 空行和标题保持不变
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue

        # 单行过长 → 在句子边界插入换行
        if len(line) > config.LONG_LINE_THRESHOLD and "。" in line:
            parts = re.split(r'(?<=[。！？])(?=[^」』\)）])', line)
            result.append(parts[0].rstrip())
            for p in parts[1:]:
                result.append(p.lstrip())
        else:
            result.append(line)

    result_text = "\n".join(result)
    # 连续空行压缩
    result_text = re.sub(r'\n{3,}', '\n\n', result_text)
    return result_text


# ═══════════════════════════════════════════════════════════════
# 表格规范化（§2.3）
# ═══════════════════════════════════════════════════════════════

def _normalize_table_cells(table_text: str) -> str:
    """规范化 Markdown 表格单元格内的空白字符和重复表头。

    Docling 导出合并单元格 XLSX 时的问题：
    1. 单元格内填充大量空格
    2. 合并单元格被复制到全部列 → 一行全是同一内容

    处理：逐 cell 压缩空格；若一行全部非空 cell 内容相同，转纯文本行，
    避免关键词在 embedding 中被反复强化。
    """
    lines = table_text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        # 分隔行（|---|---|）保持原样
        if re.match(r'^[\|\s\-:]+$', stripped):
            result.append(stripped)
            continue
        if stripped.startswith("|"):
            cells = stripped.split("|")
            normalized = []
            for cell in cells:
                cell = cell.strip()
                cell = re.sub(r'\s{2,}', ' ', cell)
                normalized.append(cell)

            # 所有非空 cell 内容相同（合并单元格产物）→ 转纯文本
            non_empty = [c for c in normalized if c]
            if len(non_empty) >= 2 and all(c == non_empty[0] for c in non_empty):
                result.append(non_empty[0])
            else:
                result.append("|".join(normalized))
        else:
            result.append(line)
    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════
# 主切分（§2.4）
# ═══════════════════════════════════════════════════════════════

def _extract_tables(text: str) -> tuple[str, list[str]]:
    """提取 Markdown 表格区域，返回 (剩余文本, 表格列表)。

    表格判定：连续两行以上以 | 开头或为分隔行 |---|...|。
    提取时自动对单元格做 whitespace normalize。
    """
    lines = text.split("\n")
    tables = []
    result_lines = []
    in_table = False
    table_lines = []

    for line in lines:
        stripped = line.strip()
        is_table_line = bool(re.match(r'^\|.*\|$', stripped))
        is_separator = bool(re.match(r'^[\|\s\-:]+$', stripped))

        if is_table_line or (in_table and is_separator):
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(line)
        else:
            if in_table:
                raw = "\n".join(table_lines)
                tables.append(_normalize_table_cells(raw))
                table_lines = []
                in_table = False
            result_lines.append(line)

    if in_table:
        raw = "\n".join(table_lines)
        tables.append(_normalize_table_cells(raw))

    return "\n".join(result_lines), tables


def _recursive_split(text: str, chunk_size: int) -> list[str]:
    """递归合拢切分：段落 → 行 → 返回（超长的留给 _fine_split 处理）。"""
    for sep in ("\n\n", "\n"):
        parts = text.split(sep)
        chunks, buf = [], ""
        for part in parts:
            candidate = (buf + sep + part).lstrip(sep) if buf else part
            if len(candidate) <= chunk_size:
                buf = candidate
            else:
                if buf:
                    chunks.append(buf)
                buf = part if len(part) <= chunk_size else ""
                if not buf:
                    chunks.append(part)
        if buf:
            chunks.append(buf)
        if all(len(c) <= chunk_size for c in chunks):
            return chunks
        # 仍有超长块，降级到更细分隔符继续
        text = "\n".join(chunks)
    return [text]  # 实在没法细切，交给 _fine_split


def _fine_split(text: str, chunk_size: int) -> list[str]:
    """对超长块做句子 → 子句 → 硬切。"""
    result = []
    for part in re.split(r'(?<=[。！？])(?!\n)', text):
        if len(part) <= chunk_size:
            result.append(part)
        else:
            for sub in re.split(r'(?<=[；，])(?!\n)', part):
                if len(sub) <= chunk_size:
                    result.append(sub)
                else:
                    result.extend(_force_split(sub, chunk_size))
    return result


def _force_split(text: str, chunk_size: int) -> list[str]:
    """硬切：优先在标点/空格处断开，否则在 chunk_size 强行切开。"""
    result = []
    while len(text) > chunk_size:
        split_at = -1
        for ch in ["。", "！", "？", "；", "，", " ", "\n"]:
            idx = text.rfind(ch, 0, chunk_size)
            if idx > chunk_size * 0.5:  # 不能太偏前
                split_at = idx + len(ch)
                break
        if split_at <= 0:
            split_at = chunk_size

        result.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()

    if text.strip():
        result.append(text)
    return result


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """块间重叠：后一块的开头从前一块结尾取 overlap 字符。

    表格 chunk（以 | 开头）不做 overlap，避免破坏表头结构。
    """
    if not chunks or overlap <= 0:
        return chunks
    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = result[-1]
        current = chunks[i]
        prev_is_table = prev.strip().startswith("|")
        current_is_table = current.strip().startswith("|")

        if prev_is_table or current_is_table:
            result.append(current)
        elif len(prev) > overlap:
            tail = prev[-overlap:]
            for ch in ["\n", "。", "！", "？", "；", "，", " "]:
                idx = tail.find(ch)
                if idx >= 0:
                    tail = tail[idx + 1:]
                    break
            result.append(tail + current)
        else:
            result.append(current)
    return result


def split_text(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    """五层递归切分（§2.4）。

    Returns:
        chunk 文本列表（不含元数据头，且已去除空白 chunk）。
    """
    chunk_size = chunk_size or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP

    # Step 0: 提取表格区域（整体保护不对其内部切分）
    text, tables = _extract_tables(text)

    # Step 1: Markdown 标题前插入双换行（语义边界）
    text = re.sub(r'(?<!\n)\n(#{1,3}\s)', r'\n\n\1', text)

    # Step 2: 段落 → 行递归合拢
    chunks = _recursive_split(text, chunk_size)

    # Step 3: 超长块精细切分
    final_chunks = []
    for c in chunks:
        if len(c) <= chunk_size:
            final_chunks.append(c)
        else:
            final_chunks.extend(_fine_split(c, chunk_size))

    # Step 4: 表格插回（表头每 chunk 复制，数据行按 chunk_size 分组）
    for tbl in tables:
        if len(tbl) <= chunk_size:
            final_chunks.append(tbl)
        else:
            lines = tbl.split("\n")
            # 表头 = 列名行 + 分隔行（两行固定，每个 chunk 都复制）
            header_lines = lines[:2]
            data_lines = lines[2:]

            buf = ""
            for row in data_lines:
                candidate = buf + "\n" + row if buf else row
                header_size = len(header_lines[0]) + len(header_lines[1]) + 2
                if header_size + len(candidate) <= chunk_size:
                    buf = candidate
                else:
                    if buf:
                        final_chunks.append(f"{header_lines[0]}\n{header_lines[1]}\n{buf}")
                    buf = row
            if buf:
                final_chunks.append(f"{header_lines[0]}\n{header_lines[1]}\n{buf}")

    # Step 5: 块间重叠（表格 chunk 跳过 overlap）
    overlapped = _apply_overlap(final_chunks, overlap)
    return [c for c in overlapped if c.strip()]


# ═══════════════════════════════════════════════════════════════
# 行号追踪 + 元数据头
# ═══════════════════════════════════════════════════════════════

def _compute_line_positions(text: str) -> list[int]:
    positions = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            positions.append(i + 1)
    return positions


def _pos_to_line(positions: list[int], pos: int) -> int:
    for i, p in enumerate(positions):
        if p > pos:
            return i
    return len(positions)


def split_document(title: str, content: str, source: str = "") -> list[dict]:
    """对一个文档切分，返回带元数据头的 chunk 列表。

    Args:
        title: 文档标题（用于元数据头，如文件名 stem）
        content: 原始文本
        source: 来源标识（如原始文件名）

    Returns:
        [{"index": int, "content": str(含元数据头), "lines": str, "source": str}, ...]
    """
    normalized = normalize_for_splitting(content)
    raw_chunks = split_text(normalized)
    chunk_texts = [c for c in raw_chunks if c.strip()]
    if not chunk_texts:
        chunk_texts = [content]

    # 构建行号位置索引
    line_positions = _compute_line_positions(normalized)
    total = len(chunk_texts)
    chunks = []
    current_pos = 0

    for i, chunk_text in enumerate(chunk_texts):
        # 在原文中定位此 chunk（处理重复文本时找下一个匹配位置）
        start_pos = normalized.find(chunk_text, current_pos)
        if start_pos < 0:
            start_pos = normalized.find(chunk_text.strip())
        end_pos = (start_pos + len(chunk_text)) if start_pos >= 0 else 0
        current_pos = max(current_pos, end_pos)

        start_line = _pos_to_line(line_positions, start_pos) if start_pos >= 0 else i + 1
        end_line = _pos_to_line(line_positions, end_pos) if end_pos > 0 else start_line

        if start_line == end_line:
            lines_label = str(start_line)
        else:
            lines_label = f"{start_line}-{end_line}"

        # 元数据头：LLM 看到后能理解上下文
        metadata_header = f"[文档: {title} | 片段 {i+1}/{total}"
        if lines_label:
            metadata_header += f" | 第{lines_label}行"
        metadata_header += "]\n\n"

        chunks.append({
            "index": i,
            "content": metadata_header + chunk_text,
            "lines": lines_label,
            "source": source,
        })

    return chunks
