"""基于解析文本块的字段自动抽取建议（人工确认辅助）。

设计原则：不替代人工核对。所有抽取结果都回带「命中原文块 index」，
前端可一键采纳并把该原文块作为 evidence 提交；错误抽取由人工手动覆盖。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_DATE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_NUM = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")

# (kind, 正则)：range=区间, max=上限, min=下限/以上, exact=固定人数
_TEAM_PATTERNS = [
    ("range", re.compile(r"(\d{1,2})\s*[-~\u2013\u2014到至]\s*(\d{1,2})\s*人")),
    ("max", re.compile(r"不超过\s*(\d{1,2})\s*人")),
    ("min", re.compile(r"(\d{1,2})\s*人(?:以上|及以上)")),
    ("exact", re.compile(r"每队[约合]?\s*(\d{1,2})\s*人")),
]

_ELIGIBLE = [
    (re.compile(r"(?<!职业)本科|本专科"), "本科生"),
    (re.compile(r"研究生|硕士|博士"), "研究生"),
    (re.compile(r"专科|本专科"), "专科生"),
    (re.compile(r"高职|高专"), "高职高专生"),
    (re.compile(r"中职"), "中职生"),
    (re.compile(r"职业本科"), "职业本科生"),
]
_TIME = re.compile(r"(?<!\d)(\d{1,2}):\s*(\d{2})(?::\s*(\d{2}))?(?!\d)")
_ELIGIBLE_CUE = re.compile(r"参赛对象|参赛资格|面向|参赛队员|参赛学生|参赛者|参赛人员")
_EXCLUSION = re.compile(r"不接受|不得|不含|不包括|除外|不允许|不能|禁止|不具备")


def extract_date(text: str) -> Optional[str]:
    """从文本抽取日期，返回 YYYY-MM-DD 或 None。"""
    matches = sorted([*_DATE_CN.finditer(text), *_DATE_NUM.finditer(text)], key=lambda m: m.start())
    for match in matches:
        try:
            return date(*(int(value) for value in match.groups())).isoformat()
        except ValueError:
            continue
    return None


def _deadline_candidate(clause: str) -> tuple[str, Optional[str]] | None:
    matches = [*_DATE_CN.finditer(clause), *_DATE_NUM.finditer(clause)]
    # Multiple dates may be a registration range or another round; do not guess its endpoint.
    if len(matches) != 1:
        return None
    value = extract_date(clause)
    if value is None:
        return None
    times = list(_TIME.finditer(clause))
    clock = None
    if len(times) == 1 and times[0].start() >= matches[0].end():
        m = times[0]
        hour, minute, second = int(m[1]), int(m[2]), int(m[3] or 0)
        if hour > 23 or minute > 59 or second > 59:
            return None
        clock = f"{hour:02d}:{minute:02d}" + (f":{second:02d}" if m[3] else "")
    return value, clock


def suggest_fields(blocks: list[dict]) -> dict:
    """输入 [{page, paragraph_index, text}]，返回字段抽取建议。

    每个字段建议含 value 与 evidence_index（命中原文块的序号），
    便于前端预关联引用。
    """
    suggestions: dict = {
        "team": {"required": False, "min": None, "max": None, "evidence_index": None},
        "registration_deadline": {"value": None, "evidence_index": None},
        "submission_deadline": {"value": None, "evidence_index": None},
        "registration_deadline_time": {"value": None, "evidence_index": None},
        "submission_deadline_time": {"value": None, "evidence_index": None},
        "eligible": {"value": None, "evidence_index": None},
    }
    deadlines: dict[str, list[tuple[str, Optional[str], int]]] = {"registration": [], "submission": []}
    eligible_values: list[str] = []
    eligible_indices: list[int] = []

    for i, b in enumerate(blocks):
        text = b.get("text", "")

        # —— 团队人数 ——
        if not suggestions["team"]["required"] and re.search(r"每队|队伍|团队|组队|参赛队|每组", text):
            for kind, pat in _TEAM_PATTERNS:
                m = pat.search(text)
                if m:
                    if kind == "range":
                        suggestions["team"]["min"] = int(m.group(1))
                        suggestions["team"]["max"] = int(m.group(2))
                    elif kind == "max":
                        suggestions["team"]["max"] = int(m.group(1))
                    elif kind == "min":
                        suggestions["team"]["min"] = int(m.group(1))
                    else:
                        suggestions["team"]["min"] = int(m.group(1))
                        suggestions["team"]["max"] = int(m.group(1))
                    minimum, maximum = suggestions["team"]["min"], suggestions["team"]["max"]
                    if (minimum is not None and minimum < 1) or (maximum is not None and (maximum < 1 or (minimum and maximum < minimum))):
                        suggestions["team"]["min"] = suggestions["team"]["max"] = None
                        continue
                    suggestions["team"]["required"] = True
                    suggestions["team"]["evidence_index"] = i
                    break

        for clause in re.split(r"[。；;\n]", text):
            registration = bool(re.search(r"报名|注册", clause))
            submission = bool(re.search(r"提交|上传|递交", clause))
            if registration != submission and re.search(r"截止|截至", clause):
                candidate = _deadline_candidate(clause)
                if candidate:
                    kind = "registration" if registration else "submission"
                    deadlines[kind].append((*candidate, i))
            if _ELIGIBLE_CUE.search(clause) and not _EXCLUSION.search(clause):
                for pattern, value in _ELIGIBLE:
                    if pattern.search(clause) and value not in eligible_values:
                        eligible_values.append(value)
                        eligible_indices.append(i)

    for kind, candidates in deadlines.items():
        if candidates and len({(value, clock) for value, clock, _ in candidates}) == 1:
            value, clock, index = candidates[0]
            suggestions[f"{kind}_deadline"] = {"value": value, "evidence_index": index}
            suggestions[f"{kind}_deadline_time"] = {"value": clock, "evidence_index": index if clock else None}
    if eligible_values:
        suggestions["eligible"] = {"value": eligible_values, "evidence_index": eligible_indices[0], "evidence_indices": sorted(set(eligible_indices))}
    return suggestions
