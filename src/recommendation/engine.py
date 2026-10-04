"""推荐引擎：数据有效性检查 -> 硬性门控（一票否决）-> 软性匹配评分。

严格遵循修订方案「三、推荐算法改为门控加评分」：
  - 第一层：数据本身是否可用（无可靠来源不进入正式推荐）
  - 第二层：资格与时间硬性门控，任一不满足则一票否决，score=null
  - 第三层：仅对通过门控的赛事计算 MatchScore = 0.40S + 0.25E + 0.20R + 0.15W
  - 推荐结果必须解释：资格/匹配/缺口/队友/时间/下一步
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re

from schemas import (
    Competition,
    DataStatus,
    GateReason,
    MatchBreakdown,
    RecommendationResult,
    TeammateMatch,
    UserProfile,
    EducationLevel,
    Grade,
    grades_for_education,
)
from fact_formatting import format_team_size
from trust import assess_source_readiness, is_registerable_now
from contest_clock import calendar_date


# ---------------------------------------------------------------------------
# 第一层：数据有效性检查
# ---------------------------------------------------------------------------


def data_validity_check(comp: Competition, current: date) -> list[str]:
    """返回阻止赛事进入正式推荐的原因；空列表表示数据可用。"""
    return list(assess_source_readiness(comp).reasons)


# ---------------------------------------------------------------------------
# 第二层：硬性门控（一票否决）
# ---------------------------------------------------------------------------


def eligibility_gate(
    user: UserProfile, comp: Competition, current: date | datetime
) -> tuple[bool, list[GateReason]]:
    """资格与时间硬条件。任一不满足即一票否决。"""
    reasons: list[GateReason] = []

    effective_deadline = comp.registration_deadline or comp.submission_deadline
    if effective_deadline is not None and effective_deadline < calendar_date(current):
        reason = "报名已经截止" if comp.registration_deadline else "作品提交已经截止"
        reasons.append(GateReason(reason=reason))

    # Normalize only known academic synonyms; never change the stored identity.
    academic_levels = {
        EducationLevel.VOCATIONAL_COLLEGE: EducationLevel.JUNIOR_COLLEGE,
        EducationLevel.VOCATIONAL_COLLEGE_STUDENT: EducationLevel.JUNIOR_COLLEGE,
        EducationLevel.VOCATIONAL_UNDERGRADUATE: EducationLevel.UNDERGRADUATE,
    }
    user_level = academic_levels.get(user.education_level, user.education_level)
    allowed_levels = {academic_levels.get(level, level) for level in comp.eligible_students}
    if user_level not in allowed_levels:
        reasons.append(GateReason(reason="学历层次不符合要求"))

    if user.grade == Grade.UNKNOWN or user.grade not in grades_for_education(user.education_level):
        reasons.append(GateReason(reason="学历与年级信息不一致或待确认", possible_action="请在能力画像中确认真实学籍与年级"))

    if comp.allowed_grades and user.grade not in comp.allowed_grades:
        reasons.append(
            GateReason(
                reason=f"年级不符合要求（仅允许 {', '.join(g.value for g in comp.allowed_grades)}）",
                possible_action="寻找允许该年级参加的同类赛事",
            )
        )

    if comp.allowed_majors:
        # 专业匹配放宽到「包含关系」，避免「计算机科学与技术」无法命中「计算机类」
        major_ok = any(user.major in m or m in user.major for m in comp.allowed_majors)
        if not major_ok:
            reasons.append(
                GateReason(
                    reason=f"专业不符合要求（仅允许 {', '.join(comp.allowed_majors)}）",
                    possible_action="寻找允许该专业参加的同类赛事",
                )
            )

    if comp.team_required and user.expected_team_size > (comp.team_max or 999):
        reasons.append(GateReason(reason="预计团队人数超过上限"))
    if comp.team_required and comp.team_min is not None and user.expected_team_size < comp.team_min:
        reasons.append(GateReason(reason="预计团队人数低于下限"))
    if not is_registerable_now(comp, current) and not any("截止" in r.reason for r in reasons):
        reasons.append(GateReason(reason="赛事当前已不可报名"))

    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# 时间门控（用户规则）：仅推送当前仍可报名的赛事
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 第三层：软性匹配评分
# ---------------------------------------------------------------------------

# 画像填写的技能名称和赛事规则中的表达并不总是完全一致。这里采用可审计的
# 轻量词族，而不是黑箱向量：每个词族都可在代码和推荐解释中追溯。
_SKILL_FAMILIES = (
    frozenset({"python", "py", "数据分析", "数据处理"}),
    frozenset({"c", "c++", "c/c++", "c语言", "程序设计"}),
    frozenset({"java", "spring", "后端开发", "后端"}),
    frozenset({"算法", "算法与数据结构", "数据结构", "动态规划", "程序设计"}),
    frozenset({"前端", "前端开发", "web", "web开发", "网页开发", "交互设计"}),
    frozenset({"机器学习", "深度学习", "pytorch", "ai", "人工智能", "nlp"}),
    frozenset({"数学建模", "matlab", "统计分析", "数学", "建模"}),
    frozenset({"ui设计", "figma", "photoshop", "海报设计", "品牌视觉", "视觉设计", "交互设计"}),
    frozenset({"嵌入式", "stm32", "硬件电路", "物联网", "传感器", "电子设计"}),
    frozenset({"商业计划书", "市场调研", "项目管理", "路演演讲", "商业分析", "创业"}),
    frozenset({"论文写作", "报告写作", "学术写作", "ppt汇报", "路演表达"}),
)

_MAJOR_CATEGORY_HINTS = {
    "计算机": {"programming", "software", "data", "robotics_ai", "modeling"},
    "软件": {"programming", "software", "data", "robotics_ai"},
    "人工智能": {"robotics_ai", "data", "programming", "modeling"},
    "数据": {"data", "modeling", "programming", "business"},
    "数学": {"math", "modeling", "data"},
    "统计": {"math", "modeling", "data", "business"},
    "电子": {"electronics", "robotics_ai", "engineering", "programming"},
    "自动化": {"electronics", "robotics_ai", "engineering"},
    "机械": {"engineering", "robotics_ai"},
    "视觉": {"design", "innovation", "software"},
    "艺术": {"design", "innovation"},
    "设计": {"design", "innovation"},
    "管理": {"business", "innovation", "data"},
    "财经": {"business", "innovation", "data"},
    "金融": {"business", "data", "innovation"},
    "英语": {"english"},
    "外语": {"english"},
    "物理": {"physics", "engineering", "electronics"},
    "化学": {"chem_env", "life_science"},
    "环境": {"chem_env", "engineering"},
    "生物": {"life_science"},
    "医学": {"life_science"},
}


def _norm(value: str) -> str:
    return re.sub(r"[\s\-_/（）()]+", "", value.lower())


def _skill_family(value: str) -> frozenset[str] | None:
    normalized = _norm(value)
    for family in _SKILL_FAMILIES:
        if any(_norm(term) in normalized or normalized in _norm(term) for term in family):
            return family
    return None


def _major_score(user: UserProfile, comp: Competition) -> float:
    major = _norm(user.major)
    if not major:
        return 50.0
    hinted_categories = {
        category for keyword, categories in _MAJOR_CATEGORY_HINTS.items()
        if _norm(keyword) in major for category in categories
    }
    if comp.category.value in hinted_categories:
        return 100.0
    # 赛事显式不限专业时仍可参加，但不把它误判为“专业高度相关”。
    if not comp.allowed_majors:
        return 55.0
    if any(_norm(allowed) in major or major in _norm(allowed) for allowed in comp.allowed_majors):
        return 100.0
    return 15.0


def _skills_match(user_skill: str, required_skill: str) -> bool:
    left, right = _norm(user_skill), _norm(required_skill)
    if left == right or left in right or right in left:
        return True
    left_family, right_family = _skill_family(user_skill), _skill_family(required_skill)
    return bool(left_family and left_family == right_family)


def _skill_score(user: UserProfile, comp: Competition) -> float:
    major_score = _major_score(user, comp)
    if not comp.required_skills:
        return round(0.7 * major_score + 0.3 * 60.0, 1)
    matched = sum(
        1 for required in comp.required_skills
        if any(_skills_match(skill, required) for skill in user.skills)
    )
    skill_score = matched / len(comp.required_skills) * 100
    # 专业相关性不取代具体技能，而是防止“技能词不完全相同”造成系统性误判。
    return round(0.72 * skill_score + 0.28 * major_score, 1)


def _experience_score(user: UserProfile, comp: Competition) -> float:
    major_score = _major_score(user, comp)
    if not user.experiences:
        return round(25.0 + 0.35 * major_score, 1)
    # 过往赛事同名、同赛道关键词和专业相关性共同决定经历分，避免只识别英文类别码。
    corpus = " ".join(user.experiences + user.skills + [user.major])
    title_terms = [term for term in re.split(r"[·—－（）()\s]+", comp.competition_name) if len(term) >= 2]
    title_hit = any(term in corpus for term in title_terms[:4])
    category_hit = _major_score(user, comp) >= 100 or any(
        _skills_match(skill, required) for skill in user.skills for required in comp.required_skills
    )
    return round(min(100.0, 25.0 + 0.40 * major_score + (25.0 if category_hit else 0.0) + (10.0 if title_hit else 0.0)), 1)


def _resource_score(user: UserProfile, comp: Competition) -> float:
    # 组队赛：现有期望人数越接近上限，资源越充足
    if not comp.team_required:
        return 90.0
    if user.expected_team_size >= (comp.team_min or 1):
        return 80.0
    return 50.0


def _workload_score(user: UserProfile, comp: Competition, current: date) -> float:
    effective_deadline = comp.registration_deadline or comp.submission_deadline
    if effective_deadline is None:
        return 70.0
    remaining_weeks = max(0, (effective_deadline - current).days / 7.0)
    available_hours = remaining_weeks * user.weekly_available_hours
    required_hours = estimate_competition_workload(comp)
    return round(min(100.0, available_hours / required_hours * 100), 1)


def estimate_competition_workload(comp: Competition) -> float:
    """估算赛事所需投入小时（启发式）。"""
    base = 40.0
    if comp.team_required:
        base += 60.0
    base += 10.0 * max(0, len(comp.required_materials) - 1)
    return base


def soft_match_score(
    user: UserProfile, comp: Competition, current: date | datetime
) -> MatchBreakdown:
    s = _skill_score(user, comp)
    e = _experience_score(user, comp)
    r = _resource_score(user, comp)
    w = _workload_score(user, comp, calendar_date(current))
    total = round(0.40 * s + 0.25 * e + 0.20 * r + 0.15 * w, 1)
    return MatchBreakdown(
        skill_score=s, experience_score=e, resource_score=r,
        workload_score=w, total=total,
    )


# ---------------------------------------------------------------------------
# 分层与解释
# ---------------------------------------------------------------------------


def _status_from_score(score: float) -> RecommendationResult.recommendation_status:  # type: ignore[valid-type]
    if score >= 85:
        return "highly_suitable"
    if score >= 70:
        return "suitable"
    if score >= 55:
        return "marginal"
    return "not_prioritized"


def _build_explanation(
    user: UserProfile, comp: Competition, breakdown: MatchBreakdown, current: date | datetime
) -> dict:
    skill_gap = [s for s in comp.required_skills if s not in user.skills]
    effective_deadline = comp.registration_deadline or comp.submission_deadline
    remaining_weeks = ((effective_deadline - calendar_date(current)).days / 7.0 if effective_deadline else None)
    return {
        "资格": "符合硬性资格与时间要求",
        "能力匹配": f"技能匹配度 {breakdown.skill_score}，经历匹配度 {breakdown.experience_score}",
        "能力缺口": f"缺少技能：{', '.join(skill_gap) if skill_gap else '无'}",
        "需要队友": (
            f"该赛事团队要求：{format_team_size(comp.team_min, comp.team_max)}"
            if comp.team_required else "个人赛，无需组队"
        ),
        "时间是否充足": (
            f"剩余约 {remaining_weeks:.1f} 周，工作量可承受度 {breakdown.workload_score}"
            if remaining_weeks is not None else "关键截止时间未知"
        ),
        "下一步": "补充缺失技能 / 组建团队 / 在截止前提交报名" if skill_gap or comp.team_required else "可直接报名",
    }


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------


def recommend_for_user(
    user: UserProfile, competitions: list[Competition], current: date | datetime
) -> list[RecommendationResult]:
    """对全部赛事执行三层推荐，返回排序后的结果列表。"""
    results: list[RecommendationResult] = []
    day = calendar_date(current)

    for comp in competitions:
        # 时间问题属于明确的一票否决，保留结果以便 API / 评测解释 score=null。
        if not is_registerable_now(comp, current):
            results.append(
                RecommendationResult(
                    competition_id=comp.competition_id,
                    competition_name=comp.competition_name,
                    recommendation_status="ineligible",
                    eligible=False,
                    gate_reasons=[GateReason(reason="赛事当前已不可报名")],
                    explanation={"时间": "赛事已截止、已开赛或已结束，不进入评分"},
                )
            )
            continue
        source_readiness = assess_source_readiness(comp)
        pending = not source_readiness.ready
        problems = list(source_readiness.reasons)
        if problems:
            # Candidate facts must not be used to assert personal eligibility.
            effective_deadline = comp.registration_deadline or comp.submission_deadline
            urgent = effective_deadline is not None and 0 <= (effective_deadline - day).days <= 14
            results.append(
                RecommendationResult(
                    competition_id=comp.competition_id,
                    competition_name=comp.competition_name,
                    recommendation_status="candidate_only",
                    score=None,
                    eligible=False,
                    match_breakdown=None,
                    explanation={
                        "数据状态": "来源或关键证据待核实，仅作为候选信息展示",
                        "待补充": problems,
                    },
                    urgent=urgent,
                    pending_review=pending,
                )
            )
            continue

        # 第二层：硬性门控
        eligible, reasons = eligibility_gate(user, comp, current)
        if not eligible:
            results.append(
                RecommendationResult(
                    competition_id=comp.competition_id,
                    competition_name=comp.competition_name,
                    recommendation_status="ineligible",
                    eligible=False,
                    gate_reasons=reasons,
                    explanation={
                        "资格": "不满足硬性资格或时间要求，一票否决",
                        "原因": [r.reason for r in reasons],
                    },
                    pending_review=pending,
                )
            )
            continue

        # 第三层：软性评分
        breakdown = soft_match_score(user, comp, current)
        status = _status_from_score(breakdown.total)
        effective_deadline = comp.registration_deadline or comp.submission_deadline
        urgent = effective_deadline is not None and 0 <= (effective_deadline - day).days <= 14
        results.append(
            RecommendationResult(
                competition_id=comp.competition_id,
                competition_name=comp.competition_name,
                recommendation_status=status,
                score=breakdown.total,
                eligible=True,
                match_breakdown=breakdown,
                explanation=_build_explanation(user, comp, breakdown, current),
                urgent=urgent,
                pending_review=pending,
            )
        )

    # Only formally eligible events have a match score.
    results.sort(
        key=lambda r: (
            0 if r.eligible else 1,
            0 if r.recommendation_status == "candidate_only" else 1,
            -(r.score or 0),
            0 if (not r.pending_review) else 1,
            not r.urgent,
        )
    )
    return results


# ---------------------------------------------------------------------------
# 队友推荐：基于画像的互补匹配
# ---------------------------------------------------------------------------


def _grade_level(grade: str) -> int:
    return {"大一": 1, "大二": 2, "大三": 3, "大四": 4, "研究生": 5}.get(grade, 2)


def _teammate_score(seeker: UserProfile, c: UserProfile) -> tuple[float, list[str]]:
    """互补度评分（0-100）与中文理由。五个维度加权：技能互补40/专业互补25/年级搭配15/时间互补10/经验匹配10。"""
    reasons: list[str] = []
    seeker_skills = {s.lower() for s in seeker.skills}
    cand_skills = {s.lower() for s in c.skills}

    # 技能互补（40%）：候选具备而 seeker 缺失的技能越多，互补度越高
    missing = cand_skills - seeker_skills
    skill_comp = 100.0 * len(missing) / len(cand_skills) if cand_skills else 0.0
    if missing:
        shown = "、".join(list({s for s in c.skills if s.lower() in missing})[:3])
        reasons.append(f"技能互补：你尚未掌握的 {shown} 正是 TA 的强项")

    # 专业互补（25%）：跨学科组队视角更全
    if seeker.major != c.major:
        major_comp = 100.0
        reasons.append(f"专业互补：你是{seeker.major}，TA 是{c.major}，跨学科组合")
    else:
        major_comp = 45.0

    # 年级搭配（15%）：经验梯度合理
    diff = abs(_grade_level(seeker.grade) - _grade_level(c.grade))
    grade_comp = 100.0 if diff >= 2 else (80.0 if diff == 1 else 60.0)
    if diff >= 1:
        reasons.append(f"年级搭配：{seeker.grade} + {c.grade}，经验梯度合理")

    # 时间互补（10%）：每周可投入工时差异越大，节奏搭配越灵活
    maxh = max(seeker.weekly_available_hours, c.weekly_available_hours, 1)
    time_comp = 100.0 * (1 - abs(seeker.weekly_available_hours - c.weekly_available_hours) / maxh)
    if abs(seeker.weekly_available_hours - c.weekly_available_hours) >= 6:
        reasons.append(f"时间互补：TA 每周可投入 {c.weekly_available_hours}h，与你形成节奏搭配")

    # 经验匹配（10%）：候选有参赛经历即加分
    if c.experiences:
        exp_comp = 75.0
        reasons.append(f"经验加持：TA 有{'、'.join(c.experiences[:2])}等经历")
    else:
        exp_comp = 45.0

    total = (
        0.40 * skill_comp
        + 0.25 * major_comp
        + 0.15 * grade_comp
        + 0.10 * time_comp
        + 0.10 * exp_comp
    )
    return round(total, 1), reasons


def recommend_teammates(
    seeker: UserProfile, candidates: list[UserProfile], top_k: int = 5
) -> list[TeammateMatch]:
    """从候选池中为 seeker 推荐互补队友，按互补度降序返回前 top_k 名。"""
    matches: list[TeammateMatch] = []
    for c in candidates:
        if c.user_id == seeker.user_id:
            continue
        score, reasons = _teammate_score(seeker, c)
        edu = c.education_level.value if hasattr(c.education_level, "value") else str(c.education_level)
        matches.append(
            TeammateMatch(
                user_id=c.user_id,
                display_name=c.display_name,
                persona=c.persona,
                avatar=c.avatar,
                major=c.major,
                grade=c.grade,
                education_level=edu,
                skills=c.skills,
                experiences=c.experiences,
                weekly_available_hours=c.weekly_available_hours,
                match_score=score,
                reasons=reasons[:4],
            )
        )
    matches.sort(key=lambda m: m.match_score, reverse=True)
    return matches[:top_k]
