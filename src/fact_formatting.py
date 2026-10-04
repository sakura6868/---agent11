"""Display helpers that preserve the distinction between unknown and known facts."""

from __future__ import annotations

from typing import Optional


def format_deadline(comp, field: str = "registration_deadline") -> str:
    timestamp = getattr(comp, field + "_at", None)
    day = getattr(comp, field, None)
    if timestamp is None:
        return day.isoformat() if day else "未明确"
    offset = timestamp.strftime("%z")
    label = "官方时区" if comp.deadline_timezone_basis == "official" else "校园时区假设"
    clock = timestamp.strftime("%H:%M:%S") if timestamp.second or timestamp.microsecond else timestamp.strftime("%H:%M")
    return f"{timestamp:%Y-%m-%d} {clock}（UTC{offset[:3]}:{offset[3:]}，{label}）"


def format_team_size(team_min: Optional[int], team_max: Optional[int]) -> str:
    """Format team bounds without inventing a missing minimum or maximum."""
    if team_min is not None and team_max is not None:
        if team_min == team_max:
            return f"{team_min} 人"
        return f"{team_min}—{team_max} 人"
    if team_min is not None:
        return f"至少 {team_min} 人"
    if team_max is not None:
        return f"最多 {team_max} 人"
    return "人数未明确"
