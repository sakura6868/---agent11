"""Central recommendation-readiness rules for competition source data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse

from schemas import Competition, DataStatus, TrustedLevel
from contest_clock import calendar_date


REQUIRED_EVIDENCE_FIELDS = frozenset(
    {
        "registration_deadline",
        "eligible_students",
        "team_min",
        "team_max",
        "required_materials",
    }
)

_SEARCH_OR_AGGREGATOR_DOMAINS = (
    "baidu.com",
    "bing.com",
    "google.com",
    "sogou.com",
    "so.com",
    "toutiao.com",
    "weixin.qq.com",
    "zhihu.com",
)


@dataclass(frozen=True)
class ReadinessAssessment:
    ready: bool
    reasons: tuple[str, ...]


def _is_direct_source_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    if parsed.path.lower().rstrip("/") in {"/search", "/s", "/websearch"}:
        return False
    return not any(host == domain or host.endswith(f".{domain}") for domain in _SEARCH_OR_AGGREGATOR_DOMAINS)


def _is_pdf_evidence(document_name: str | None, source_url: str | None) -> bool:
    candidates = (document_name or "", urlparse(source_url or "").path)
    return any(PurePosixPath(value.lower()).suffix == ".pdf" for value in candidates if value)


def _evidence_is_complete(item) -> bool:
    if not item.source_text.strip() or not _is_direct_source_url(item.source_url):
        return False
    if item.trusted_level != TrustedLevel.A or item.anchor_status in {"invalid", "needs_review"}:
        return False
    try:
        acquired = date.fromisoformat(item.acquired_date or "")
        checked = date.fromisoformat(item.last_verified_at or "")
    except (TypeError, ValueError):
        return False
    if checked < acquired:
        return False
    if _is_pdf_evidence(item.document_name, item.source_url) and (
        type(item.page) is not int or item.page < 1
    ):
        return False
    return True


def is_registerable_now(comp: Competition, current: date | datetime) -> bool:
    """Return whether registration is still plausibly open on ``current``."""
    day = calendar_date(current)
    precise = comp.registration_deadline_at
    if precise is not None:
        if isinstance(current, datetime):
            if current >= precise:
                return False
        elif precise.date() <= day:
            # A date alone cannot establish whether the exact cutoff has passed.
            return False
        if comp.competition_end_date is not None and comp.competition_end_date < day:
            return False
        return True
    if comp.competition_end_date is not None and comp.competition_end_date < day:
        return False
    if comp.registration_deadline is not None:
        # A production phase can start while registration remains open.
        return day <= comp.registration_deadline
    if comp.competition_start_date is not None and comp.competition_start_date <= day:
        return False
    if (
        comp.registration_deadline is None
        and comp.submission_deadline is not None
        and comp.submission_deadline < day
    ):
        return False
    if comp.registration_deadline is None and comp.submission_deadline_at is not None:
        if isinstance(current, datetime) and current >= comp.submission_deadline_at:
            return False
        if not isinstance(current, datetime) and comp.submission_deadline_at.date() <= day:
            return False
    return True


def assess_source_readiness(comp: Competition) -> ReadinessAssessment:
    """Require confirmed official provenance and complete field-level evidence."""
    reasons: list[str] = []
    if comp.data_status != DataStatus.VERIFIED:
        reasons.append("关键证据尚未通过来源确认")
    if comp.trusted_level != TrustedLevel.A:
        reasons.append("可信等级未达到 A")
    if comp.official_source_status != "found":
        reasons.append("未找到已确认的官方来源")
    if not _is_direct_source_url(comp.official_source_url):
        reasons.append("缺少直接官方页面或文件链接")
    if not comp.eligible_students:
        reasons.append("缺少明确参赛对象")
    if comp.registration_deadline is None:
        reasons.append("缺少有效报名时间")
    if comp.team_min is None or comp.team_max is None:
        reasons.append("团队人数范围尚不明确")
    elif comp.team_min < 1 or comp.team_max < comp.team_min:
        reasons.append("团队人数范围无效")
    if not comp.required_materials or not any(item.strip() for item in comp.required_materials):
        reasons.append("材料清单尚不明确")
    complete_fields = {item.field for item in comp.evidence if _evidence_is_complete(item)}
    for field in ("registration_deadline_at", "submission_deadline_at"):
        if getattr(comp, field) is not None and field not in complete_fields:
            reasons.append(f"缺少精确截止时刻的官方原文证据：{field}")
    if (
        comp.deadline_timezone_basis == "official"
        and (comp.registration_deadline_at or comp.submission_deadline_at)
        and "deadline_timezone" not in complete_fields
    ):
        reasons.append("缺少官方时区依据")
    for field in sorted(REQUIRED_EVIDENCE_FIELDS - complete_fields):
        reasons.append(f"缺少完整官方字段证据：{field}")
    return ReadinessAssessment(ready=not reasons, reasons=tuple(reasons))


def assess_recommendation_readiness(
    comp: Competition, current: date | datetime | None = None
) -> ReadinessAssessment:
    """Assess source trust and, when supplied, whether the event is still open."""
    source = assess_source_readiness(comp)
    reasons = list(source.reasons)
    if current is not None and not is_registerable_now(comp, current):
        reasons.append("当前已不可报名")
    return ReadinessAssessment(ready=not reasons, reasons=tuple(reasons))
