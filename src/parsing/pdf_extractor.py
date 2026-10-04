"""文档解析：提取文本 + 保留页码与段落信息。

为后续「结构化抽取」与「原文证据定位（页码 + 段落）」提供基础。
不同年份通知作为不同版本保存，绝不直接覆盖（见 data/raw 目录约定）。
"""

from __future__ import annotations

import re
import sys
import hashlib
from pathlib import Path
from typing import Optional

# 允许脚本直接运行（python parsing/pdf_extractor.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CompetitionCategory, EvidenceRect, ParsedBlock, ParseResult

# 段落切分：以空行或明显缩进变化作为分段依据，退化为按换行切分
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n|\n(?=\s{4,})")


def parse_pdf(
    pdf_path: str | Path,
    category: CompetitionCategory,
    document_year: int,
    competition_id: Optional[str] = None,
    document_id: Optional[int] = None,
) -> ParseResult:
    """解析 PDF，返回带页码与段落序号的文本块列表。

    依赖：pdfplumber（已列入 requirements）。无pdfplumber时退化为空结果并打印提示。
    """
    import pdfplumber

    pdf_path = Path(pdf_path)
    blocks: list[ParsedBlock] = []

    document_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=True, keep_blank_chars=False) or []
            positioned_lines: list[tuple[str, list[EvidenceRect]]] = []
            if words and page.width and page.height:
                ordered = sorted(words, key=lambda w: (round(float(w["top"]) / 3), float(w["x0"])))
                lines: list[list[dict]] = []
                for word in ordered:
                    if not lines or abs(float(word["top"]) - float(lines[-1][0]["top"])) > 3.0:
                        lines.append([word])
                    else:
                        lines[-1].append(word)
                for line in lines:
                    line.sort(key=lambda w: float(w["x0"]))
                    text = " ".join(str(w.get("text", "")).strip() for w in line).strip()
                    if not text:
                        continue
                    x0 = min(float(w["x0"]) for w in line) / float(page.width)
                    x1 = max(float(w["x1"]) for w in line) / float(page.width)
                    top = min(float(w["top"]) for w in line) / float(page.height)
                    bottom = max(float(w["bottom"]) for w in line) / float(page.height)
                    positioned_lines.append(
                        (
                            text,
                            [
                                EvidenceRect(
                                    x0=max(0.0, min(1.0, x0)),
                                    top=max(0.0, min(1.0, top)),
                                    x1=max(0.0, min(1.0, x1)),
                                    bottom=max(0.0, min(1.0, bottom)),
                                )
                            ],
                        )
                    )
            if not positioned_lines:
                text = page.extract_text() or ""
                positioned_lines = [
                    (paragraph, [])
                    for paragraph in (p.strip() for p in _PARAGRAPH_SPLIT.split(text))
                    if paragraph
                ]
            for para_index, (para, rects) in enumerate(positioned_lines, start=1):
                blocks.append(
                    ParsedBlock(
                        page=page_index,
                        paragraph_index=para_index,
                        text=para,
                        competition_id=competition_id,
                        rects=rects,
                        anchor_quality="exact" if rects else "page_only",
                    )
                )

    return ParseResult(
        document_name=pdf_path.name,
        category=category,
        document_year=document_year,
        blocks=blocks,
        document_id=document_id,
        document_sha256=document_sha256,
    )


def parse_docx(
    docx_path: str | Path,
    category: CompetitionCategory,
    document_year: int,
    competition_id: Optional[str] = None,
    document_id: Optional[int] = None,
) -> ParseResult:
    """解析 Word 文档，按段落保留序号（页码在 docx 中不可靠，统一记为 0）。"""
    from docx import Document

    docx_path = Path(docx_path)
    blocks: list[ParsedBlock] = []

    doc = Document(str(docx_path))
    for para_index, para in enumerate(doc.paragraphs, start=1):
        text = para.text.strip()
        if text:
            blocks.append(
                ParsedBlock(
                    page=0,
                    paragraph_index=para_index,
                    text=text,
                    competition_id=competition_id,
                )
            )

    return ParseResult(
        document_name=docx_path.name,
        category=category,
        document_year=document_year,
        blocks=blocks,
        document_id=document_id,
        document_sha256=hashlib.sha256(docx_path.read_bytes()).hexdigest(),
    )


def locate_evidence(parse_result: ParseResult, keyword: str) -> Optional[ParsedBlock]:
    """给定关键词，定位首个命中的文本块，用于来源检查与证据抽取。"""
    for block in parse_result.blocks:
        if keyword in block.text:
            return block
    return None


if __name__ == "__main__":
    # 简单自测：把第一个参数当 PDF 路径解析并打印块数
    import sys

    if len(sys.argv) > 1:
        res = parse_pdf(
            sys.argv[1], CompetitionCategory.PROGRAMMING, 2026
        )
        print(f"解析 {res.document_name}：共 {len(res.blocks)} 个文本块")
        for b in res.blocks[:3]:
            print(f"  [p{b.page}-¶{b.paragraph_index}] {b.text[:60]}")
