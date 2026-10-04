"""把已有字段级引用与雷达归档的官方 PDF 精确对齐。"""

from __future__ import annotations

import re
import unicodedata
from io import BytesIO

import db


def _queries(source_text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", source_text or "").strip()
    if not clean:
        return []
    candidates = [clean]
    if len(clean) > 48:
        candidates.append(clean[:48])
    if len(clean) > 28:
        candidates.append(clean[:28])
    return list(dict.fromkeys(candidates))


def _normalise_anchor(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return "".join(ch for ch in value if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _quote_context(page_text: str, exact: str) -> tuple[str, str, str, int | None, int | None]:
    """生成 TextQuote + TextPosition 多锚点；不改变证据原文。"""
    page_text = page_text or ""
    exact = re.sub(r"\s+", " ", exact or "").strip()
    collapsed = re.sub(r"\s+", " ", page_text)
    index = collapsed.casefold().find(exact.casefold()) if exact else -1
    if index < 0:
        for query in _queries(exact)[1:]:
            index = collapsed.casefold().find(query.casefold())
            if index >= 0:
                exact = collapsed[index:index + len(query)]
                break
    if index < 0:
        return exact, "", "", None, None
    end = index + len(exact)
    return exact, collapsed[max(0, index - 48):index], collapsed[end:end + 48], index, end


def _approximate_word_rects(page, source_text: str) -> tuple[list[dict], bool]:
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False) or []
    stream = ""
    spans: list[tuple[int, int, dict]] = []
    for word in words:
        token = _normalise_anchor(str(word.get("text", "")))
        if not token:
            continue
        start = len(stream)
        stream += token
        spans.append((start, len(stream), word))
    target = _normalise_anchor(source_text)
    if not target or not stream:
        return [], False
    matched = None
    exact = False
    for length in (len(target), min(64, len(target)), min(40, len(target)), min(24, len(target)), min(14, len(target))):
        if length < 10:
            continue
        fragment = target[:length]
        index = stream.find(fragment)
        if index >= 0:
            matched = (index, index + length)
            exact = length == len(target)
            break
    if matched is None:
        return [], False
    selected = [word for start, end, word in spans if end > matched[0] and start < matched[1]]
    if not selected:
        return [], False
    # 按视觉行生成多个矩形，避免跨行时覆盖大片无关区域。
    selected.sort(key=lambda word: (float(word["top"]), float(word["x0"])))
    lines: list[list[dict]] = []
    for word in selected:
        if not lines or abs(float(word["top"]) - float(lines[-1][0]["top"])) > 4.0:
            lines.append([word])
        else:
            lines[-1].append(word)
    rects = []
    for line in lines:
        rects.append(
            {
                "x0": min(float(word["x0"]) for word in line) / float(page.width),
                "top": min(float(word["top"]) for word in line) / float(page.height),
                "x1": max(float(word["x1"]) for word in line) / float(page.width),
                "bottom": max(float(word["bottom"]) for word in line) / float(page.height),
            }
        )
    return rects, exact


def enrich_competition_citations(
    competition_id: str,
    document_id: int,
    source_url: str,
) -> int:
    """仅在原文确切命中同一官方 URL 时写入坐标，不改变任何赛事事实。"""
    document = db.get_document(document_id, include_content=True)
    comp = db.get_competition(competition_id)
    if document is None or comp is None or document["mime_type"] != "application/pdf":
        return 0
    import pdfplumber

    updated = 0
    with pdfplumber.open(BytesIO(document["content"])) as pdf:
        for citation in comp.evidence:
            if citation.citation_id is None or citation.source_url != source_url:
                continue
            page_numbers = [citation.page] if citation.page and citation.page > 0 else list(range(1, len(pdf.pages) + 1))
            match = None
            matched_page = None
            matched_page_number = None
            approximate_rects: list[dict] = []
            approximate_exact = False
            for page_number in page_numbers:
                if page_number > len(pdf.pages):
                    continue
                page = pdf.pages[page_number - 1]
                for query in _queries(citation.source_text):
                    try:
                        hits = page.search(query, regex=False, case=False, return_chars=False) or []
                    except Exception:
                        hits = []
                    if hits:
                        match = hits[0]
                        matched_page = page
                        matched_page_number = page_number
                        break
                if match:
                    break
                approximate_rects, approximate_exact = _approximate_word_rects(page, citation.source_text)
                if approximate_rects:
                    matched_page = page
                    matched_page_number = page_number
                    break
            if match and matched_page is not None:
                rects = [{
                    "x0": max(0.0, min(1.0, float(match["x0"]) / float(matched_page.width))),
                    "top": max(0.0, min(1.0, float(match["top"]) / float(matched_page.height))),
                    "x1": max(0.0, min(1.0, float(match["x1"]) / float(matched_page.width))),
                    "bottom": max(0.0, min(1.0, float(match["bottom"]) / float(matched_page.height))),
                }]
                quality = "exact"
                confidence = 1.0
            elif approximate_rects:
                rects = approximate_rects
                quality = "exact" if approximate_exact else "approximate"
                confidence = 0.88 if approximate_exact else 0.68
            else:
                if citation.document_sha256 and citation.document_sha256 != document["sha256"]:
                    db.mark_citation_anchor_status(citation.citation_id, "needs_review", 0.0)
                continue
            exact, prefix, suffix, start, end = _quote_context(
                matched_page.extract_text() or "",
                citation.text_exact or citation.source_text,
            )
            status = (
                "relocated"
                if citation.document_sha256 and citation.document_sha256 != document["sha256"]
                else "original"
            )
            if db.update_citation_locator(
                citation.citation_id,
                document_id,
                document["sha256"],
                rects,
                quality,
                page=matched_page_number,
                text_exact=exact or citation.source_text,
                text_prefix=prefix,
                text_suffix=suffix,
                text_start=start,
                text_end=end,
                anchor_confidence=confidence,
                anchor_status=status,
            ):
                updated += 1
    return updated
