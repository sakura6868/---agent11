from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from schemas import Competition  # noqa: E402
from agent.graph import _resolve_competition_versioned  # noqa: E402
from api import list_competitions  # noqa: E402


def test_internet_plus_alias_resolves_to_canonical_name():
    """CHALLENGE_PROBE c15：口语「互联网+」应能命中库内正式名「中国国际大学生创新大赛」。"""
    comps = [Competition(competition_id="alias_fixture", competition_name="中国国际大学生创新大赛",
                         document_year=2026, category="innovation")]
    resolved, _note, _clar = _resolve_competition_versioned(
        "互联网+大赛我一个文科生能玩吗", comps
    )
    assert resolved is not None, "互联网+ 应解析到具体赛事，而非落到通用 chat"
    assert "中国国际大学生创新大赛" in (resolved.competition_name or "")


def test_internet_plus_alias_does_not_break_other_competitions():
    """别名展开不应影响无关赛事的解析（如蓝桥杯）。"""
    comps = db.list_competitions()
    resolved, _note, _clar = _resolve_competition_versioned(
        "2026年蓝桥杯到底难不难我零基础能冲吗", comps
    )
    assert resolved is not None
    assert "蓝桥杯" in (resolved.competition_name or "")


def test_competitions_pagination_slices_when_limit_given():
    """分页为可选能力：传 limit 应截断；不传应返回全量（前端兼容）。"""
    full = list_competitions()
    assert len(full) >= 100
    page = list_competitions(limit=10)
    assert len(page) == 10
    page2 = list_competitions(limit=10, offset=5)
    assert len(page2) == 10
    # 默认全量（不传 limit）数量与显式大 limit 一致
    assert len(list_competitions()) == len(list_competitions(limit=1000))
