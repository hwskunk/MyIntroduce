"""
文档提取 — 从 data/ 下的文件解析为纯文本（Markdown 优先）

策略（对齐 SellerAgent §2.1-2.3）：
- md/txt: 直接 UTF-8 直读（不走 Docling）
- 其他格式 (pdf/docx/xlsx/pptx/html/图片): Docling 统一解析为 Markdown
- 扫描件/纯图片: RapidOCR（Docling OCR 开启）

本模块只负责"文件 → 文本"，不涉及任何切分/规范化。
"""
import os
import re
from pathlib import Path

import config

SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx",
    ".html", ".htm", ".png", ".jpg", ".jpeg", ".bmp", ".tiff",
}

# Docling 转换器单例（收敛在本模块内）
_converter = None


def _get_converter():
    """获取 Docling 转换器单例。

    OCR 开启（RapidOCR）、表格用 FAST 模式（ACCURATE 在 CPU 上每页多花 5-10s）。
    """
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = True
        pipeline_options.do_table_structure = True
        pipeline_options.table_structure_options.mode = TableFormerMode.FAST
        pipeline_options.accelerator_options.num_threads = 4

        _converter = DocumentConverter(
            format_options={
                "pdf": PdfFormatOption(pipeline_options=pipeline_options),
            }
        )
    return _converter


def scan_files(file_filter: set[str] | None = None) -> list[Path]:
    """递归扫描 data/ 下所有支持格式的文件。

    - 跳过以 _ 开头的子目录（如 _待填写：草稿/模板区，内容未填写前不入库）
    Args:
        file_filter: 可选，只返回 basename 在此集合中的文件（用于增量加载）。
    """
    files = []
    for root, dirs, names in os.walk(config.DATA_DIR, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for name in names:
            suffix = Path(name).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                continue
            if file_filter is not None and name not in file_filter:
                continue
            files.append(Path(root) / name)
    return sorted(files, key=lambda p: str(p))


def parse_file(fpath: Path) -> str:
    """解析单个文件为纯文本（md/txt 直读，其余走 Docling）。"""
    fpath = Path(fpath)
    suffix = fpath.suffix.lower()

    # md/txt 直接读取，无需 Docling
    if suffix in (".txt", ".md"):
        with open(fpath, "r", encoding="utf-8") as f:
            return f.read()

    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"不支持的文件格式: {suffix}\n"
            f"支持的格式: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    converter = _get_converter()
    result = converter.convert(str(fpath))
    md = result.document.export_to_markdown()

    # 图片/扫描件无文字兜底
    if not md.strip() or md.strip() in {"<!-- image -->", "<!-- image -->\n"}:
        raise ValueError(f"该文件未检测到文字内容，无法提取信息: {fpath.name}")

    return md
