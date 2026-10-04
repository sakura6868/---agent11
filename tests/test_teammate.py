"""基于画像的队友推荐引擎测试。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from schemas import EducationLevel, Grade, UserProfile
from recommendation.engine import recommend_teammates, _teammate_score


def _profile(**kw) -> UserProfile:
    base = dict(
        user_id="x",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SOPHOMORE,
        major="计算机科学与技术",
        skills=["Python", "C/C++", "算法", "Java"],
        experiences=["蓝桥杯省赛"],
        weekly_available_hours=12,
        expected_team_size=3,
        privacy_consent=True,
    )
    base.update(kw)
    return UserProfile(**base)


def test_excludes_self():
    seeker = _profile(user_id="me")
    other = _profile(user_id="you", major="视觉传达设计")
    matches = recommend_teammates(seeker, [seeker, other], top_k=5)
    assert [m.user_id for m in matches] == ["you"]


def test_sorts_by_complementarity_desc():
    seeker = _profile(user_id="me", skills=["Python", "C/C++", "算法", "Java"])
    design = _profile(user_id="u_design", major="视觉传达设计",
                      skills=["UI设计", "Figma", "Photoshop"], grade=Grade.JUNIOR)
    clone = _profile(user_id="u_clone", major="计算机科学与技术",
                     skills=["Python", "C/C++", "算法", "Java"], grade=Grade.SOPHOMORE)
    matches = recommend_teammates(seeker, [clone, design], top_k=5)
    assert matches[0].user_id == "u_design"
    assert matches[0].match_score >= matches[1].match_score


def test_cross_major_scores_higher_than_same_major():
    seeker = _profile(user_id="me")
    diff_major = _profile(user_id="a", major="工商管理", skills=["商业计划书"])
    same_major = _profile(user_id="b", major="计算机科学与技术", skills=["深度学习"])
    s_diff, _ = _teammate_score(seeker, diff_major)
    s_same, _ = _teammate_score(seeker, same_major)
    assert s_diff > s_same


def test_reasons_present_and_human_readable():
    seeker = _profile(user_id="me", skills=["Python", "C/C++"])
    cand = _profile(user_id="u", major="视觉传达设计", skills=["UI设计", "Figma"])
    matches = recommend_teammates(seeker, [cand], top_k=3)
    assert matches[0].reasons
    assert any("互补" in r for r in matches[0].reasons)
