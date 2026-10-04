"""核心数据结构契约（五条开发轨道共享）。

本文件冻结三类核心 Schema：
  1. Competition  —— 赛事结构化信息（来自官方通知抽取 + 人工 Ground Truth）
  2. UserProfile  —— 用户画像（前端填写，隐私授权后保存）
  3. Citation     —— 引用证据（字段 -> 页码 + 原文 + 来源 + 核验信息）

所有 API 响应包裹结构（CompetitionDetail、RecommendationResult 等）也在此定义，
保证前端、Agent、后端在「第零周」即可并行开发。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------


class CompetitionCategory(str, Enum):
    """赛事类别，覆盖高校主流学科竞赛方向。"""

    PROGRAMMING = "programming"       # 程序设计类
    MODELING = "modeling"             # 数学建模类
    INNOVATION = "innovation"         # 创新创业类
    SOFTWARE = "software"             # 软件作品类
    ENGLISH = "english"               # 外语/英语类
    MATH = "math"                     # 数学竞技类（非建模）
    ELECTRONICS = "electronics"       # 电子设计类
    ROBOTICS_AI = "robotics_ai"       # 机器人与人工智能类
    DATA = "data"                     # 数据科学类
    DESIGN = "design"                 # 艺术与设计类
    BUSINESS = "business"             # 财经商科类
    ENGINEERING = "engineering"       # 机械与工程类
    LIFE_SCIENCE = "life_science"     # 生命科学/医药类
    PHYSICS = "physics"               # 物理类
    CHEM_ENV = "chem_env"             # 化工/环境/能源类
    LOGISTICS = "logistics"           # 物流与供应链类


class EducationLevel(str, Enum):
    UNDERGRADUATE = "本科生"
    POSTGRADUATE = "研究生"
    JUNIOR_COLLEGE = "专科生"
    VOCATIONAL_COLLEGE = "高职高专生"
    VOCATIONAL_COLLEGE_STUDENT = "高职高专学生"
    SECONDARY_VOCATIONAL = "中职生"
    VOCATIONAL_UNDERGRADUATE = "职业本科生"
    RECENT_GRADUATE = "毕业生（毕业5年内）"


class Grade(str, Enum):
    FRESHMAN = "大一"
    SOPHOMORE = "大二"
    JUNIOR = "大三"
    SENIOR = "大四"
    FIFTH_YEAR = "大五"
    MASTER_FIRST = "研一"
    MASTER_SECOND = "研二"
    MASTER_THIRD = "研三"
    MASTER_FOURTH = "研四及以上"
    DOCTOR_FIRST = "博一"
    DOCTOR_SECOND = "博二"
    DOCTOR_THIRD = "博三"
    DOCTOR_FOURTH = "博四及以上"
    GRADUATED = "已毕业"
    UNKNOWN = "待确认"


def grades_for_education(education: EducationLevel) -> tuple[Grade, ...]:
    if education == EducationLevel.POSTGRADUATE:
        return (Grade.MASTER_FIRST, Grade.MASTER_SECOND, Grade.MASTER_THIRD, Grade.MASTER_FOURTH,
                Grade.DOCTOR_FIRST, Grade.DOCTOR_SECOND, Grade.DOCTOR_THIRD, Grade.DOCTOR_FOURTH)
    if education == EducationLevel.RECENT_GRADUATE:
        return (Grade.GRADUATED,)
    grades = (Grade.FRESHMAN, Grade.SOPHOMORE, Grade.JUNIOR)
    if education in {EducationLevel.UNDERGRADUATE, EducationLevel.VOCATIONAL_UNDERGRADUATE}:
        return (*grades, Grade.SENIOR, Grade.FIFTH_YEAR)
    return grades


class TrustedLevel(str, Enum):
    """数据可信等级：A=官网来源与关键证据完整，B=部分完整，C=待核验。"""

    A = "A"
    B = "B"
    C = "C"


class DataStatus(str, Enum):
    VERIFIED = "verified"         # 官网来源已确认且通过自动完整性检查
    UNVERIFIED = "unverified"     # 关键证据待补充
    STALE = "stale"               # 来源长期未复查


class ProjectStatus(str, Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ProjectItemType(str, Enum):
    TASK = "task"
    MATERIAL = "material"


class ProjectItemStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class FactTag(str, Enum):
    """前端区分「事实 / 计算 / 建议 / 待确认」的标签。"""

    OFFICIAL = "官方规则"
    SYSTEM = "系统计算"
    SUGGESTION = "智能建议"
    PENDING = "关键证据待补充"


# ---------------------------------------------------------------------------
# 引用证据（Citation / Evidence）
# ---------------------------------------------------------------------------


class EvidenceRect(BaseModel):
    """页面内的归一化矩形坐标，原点位于左上角，取值范围 0-1。"""

    x0: float = Field(..., ge=0, le=1)
    top: float = Field(..., ge=0, le=1)
    x1: float = Field(..., ge=0, le=1)
    bottom: float = Field(..., ge=0, le=1)


class Citation(BaseModel):
    """单条字段级引用证据，用于前端角标与原文定位。"""

    citation_id: Optional[int] = Field(None, description="数据库引用 ID；种子数据中可为空")
    field: str = Field(..., description="被佐证的结构化字段名，如 team_max")
    page: Optional[int] = Field(None, description="来源页码；无法定位时返回 null")
    source_text: str = Field(..., description="官方通知中的原文片段")
    document_name: Optional[str] = Field(None, description="文档名称，如 蓝桥杯_2026_官方通知.pdf")
    source_url: Optional[str] = Field(None, description="官方链接")
    acquired_date: Optional[str] = Field(None, description="数据获取日期 YYYY-MM-DD")
    last_verified_at: Optional[str] = Field(None, description="最后来源检查日期 YYYY-MM-DD")
    trusted_level: TrustedLevel = Field(TrustedLevel.A, description="本条证据可信等级")
    document_id: Optional[int] = Field(None, description="不可变来源文档 ID")
    document_sha256: Optional[str] = Field(None, description="来源文档内容指纹")
    rects: list[EvidenceRect] = Field(default_factory=list, description="原文在 PDF 页内的高亮矩形")
    anchor_quality: Literal["exact", "approximate", "page_only"] = Field(
        "page_only", description="证据定位精度"
    )
    text_exact: Optional[str] = Field(None, description="用于跨版本重定位的精确文本锚点")
    text_prefix: Optional[str] = Field(None, description="精确文本之前的上下文锚点")
    text_suffix: Optional[str] = Field(None, description="精确文本之后的上下文锚点")
    text_start: Optional[int] = Field(None, ge=0, description="锚点在当前页规范化文本中的起点")
    text_end: Optional[int] = Field(None, ge=0, description="锚点在当前页规范化文本中的终点")
    anchor_confidence: float = Field(0.0, ge=0, le=1, description="确定性锚点匹配置信度")
    anchor_status: Literal["original", "relocated", "needs_review", "invalid"] = Field(
        "original", description="原版有效、自动重定位、需人工确认或已失效"
    )


# ---------------------------------------------------------------------------
# 赛事（Competition）
# ---------------------------------------------------------------------------


class TimelineItem(BaseModel):
    """赛事时间轴节点。"""

    label: str
    event_date: Optional[date] = None
    date_text: Optional[str] = None


class AwardDistributionItem(BaseModel):
    """奖项与比例条目。"""

    award: str
    proportion: Optional[str] = None
    note: Optional[str] = None


class RequirementItem(BaseModel):
    """赛事要求条目，带事实标签与可选引用。"""

    tag: FactTag = FactTag.OFFICIAL
    text: str
    citation: Optional[Citation] = None


class SourceItem(BaseModel):
    """来源记录。"""

    name: str
    url: Optional[str] = None
    acquired_date: Optional[str] = None
    trusted_level: TrustedLevel = TrustedLevel.A


class Verification(BaseModel):
    """核验状态包裹，统一出现在赛事详情响应中。"""

    status: DataStatus = DataStatus.UNVERIFIED
    last_verified_at: Optional[str] = None
    trusted_level: TrustedLevel = TrustedLevel.C
    note: Optional[str] = None


class Competition(BaseModel):
    """赛事结构化信息（来自官方通知抽取 + 可追溯 Ground Truth）。"""

    competition_id: str = Field(..., description="全局唯一 ID，如 lanqiao_2026")
    competition_name: str
    document_year: int = Field(..., description="通知所属年份（不同年份不得混用）")
    category: CompetitionCategory
    organizer: Optional[str] = None

    # —— 资格相关（硬性门控字段）——
    eligible_students: list[EducationLevel] = Field(default_factory=list)
    allowed_grades: Optional[list[Grade]] = None          # null = 不限年级
    allowed_majors: Optional[list[str]] = None            # null = 不限专业
    team_required: bool = False
    team_min: Optional[int] = None
    team_max: Optional[int] = None

    # —— 时间相关 ——
    registration_deadline: Optional[date] = None
    submission_deadline: Optional[date] = None
    registration_deadline_at: Optional[datetime] = None
    submission_deadline_at: Optional[datetime] = None
    deadline_timezone_basis: Literal["official", "campus_default"] = "campus_default"
    result_announcement_date: Optional[date] = None  # 成绩公布日期
    competition_start_date: Optional[date] = None    # 比赛开始日期
    competition_end_date: Optional[date] = None      # 比赛结束日期

    # —— 奖项设置 ——
    award_settings: Optional[str] = None             # 奖项设置文本说明
    award_distribution: list[AwardDistributionItem] = Field(default_factory=list)  # 各奖项比例

    # —— 比赛说明 ——
    brief_description: Optional[str] = None          # 比赛简要说明

    # —— 材料与能力 ——
    required_materials: list[str] = Field(default_factory=list)
    evaluation_dimensions: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)

    # —— 来源与可信 ——
    official_source_url: Optional[str] = None
    source_acquired_date: Optional[str] = None
    trusted_level: TrustedLevel = TrustedLevel.C
    data_status: DataStatus = DataStatus.UNVERIFIED
    last_verified_at: Optional[str] = None

    # —— 官方来源核实结论 ——
    official_source_status: Optional[str] = None  # "found" | "not_found" | None
    notes: Optional[str] = None

    # —— 证据 ——
    evidence: list[Citation] = Field(default_factory=list)

    # —— 文档版本隔离用 ——
    doc_version: str = Field("1.0", description="同一赛事同一年份的文档版本，如 2026_v1")

    @model_validator(mode="after")
    def consistent_deadline_timestamps(self):
        for name in ("registration_deadline", "submission_deadline"):
            timestamp = getattr(self, name + "_at")
            if timestamp is None:
                continue
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError(f"{name}_at requires an explicit UTC offset")
            if getattr(self, name) != timestamp.date():
                raise ValueError(f"{name}_at must match the official date field")
            if self.deadline_timezone_basis == "campus_default" and timestamp.utcoffset() != timedelta(hours=8):
                raise ValueError("campus_default requires UTC+08:00")
        return self

    def is_registration_open(self, current: date) -> bool:
        return self.registration_deadline is not None and self.registration_deadline >= current


# ---------------------------------------------------------------------------
# 用户画像（UserProfile）
# ---------------------------------------------------------------------------


class UserProfile(BaseModel):
    """用户画像。隐私授权前不得保存个性化字段。"""

    user_id: str
    education_level: EducationLevel
    grade: Grade
    major: str
    skills: list[str] = Field(default_factory=list)
    experiences: list[str] = Field(default_factory=list, description="过往参赛经历，如 蓝桥杯省赛")
    weekly_available_hours: int = Field(10, ge=0, le=168)
    expected_team_size: int = Field(1, ge=1, le=10)
    privacy_consent: bool = Field(False, description="是否已勾选隐私授权")

    # —— 登录/展示用（可选，不参与门控与评分）——
    display_name: Optional[str] = Field(None, description="昵称，用于登录页与顶栏展示")
    persona: Optional[str] = Field(None, description="一句话特色，如「算法竞赛型选手」")
    avatar: Optional[str] = Field(None, description="头像 emoji，用于登录卡片")

    def can_store_profile(self) -> bool:
        return self.privacy_consent


class TeammateMatch(BaseModel):
    """基于画像的互补队友推荐结果。"""

    user_id: str
    display_name: Optional[str] = None
    persona: Optional[str] = None
    avatar: Optional[str] = None
    major: str
    grade: str
    education_level: str
    skills: list[str] = Field(default_factory=list)
    experiences: list[str] = Field(default_factory=list)
    weekly_available_hours: int = 10
    match_score: float = Field(0.0, description="互补匹配度 0-100")
    reasons: list[str] = Field(default_factory=list, description="互补理由（中文）")


# ---------------------------------------------------------------------------
# API 响应包裹（第零周冻结的接口契约）
# ---------------------------------------------------------------------------


class CompetitionDetail(BaseModel):
    """赛事详情接口统一返回结构。"""

    competition: Competition
    timeline: list[TimelineItem] = Field(default_factory=list)
    requirements: list[RequirementItem] = Field(default_factory=list)
    sources: list[SourceItem] = Field(default_factory=list)
    verification: Verification
    recommendation_ready: bool = False
    readiness_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 推荐结果（门控 + 评分）
# ---------------------------------------------------------------------------


class GateReason(BaseModel):
    """门控不通过的单条原因。"""

    reason: str
    possible_action: Optional[str] = None


class MatchBreakdown(BaseModel):
    """软性匹配评分拆解。"""

    skill_score: float = Field(..., description="S 技能匹配度 0-100")
    experience_score: float = Field(..., description="E 经历匹配度 0-100")
    resource_score: float = Field(..., description="R 资源匹配度 0-100")
    workload_score: float = Field(..., description="W 工作量可承受度 0-100")
    total: float = Field(..., description="0.40S+0.25E+0.20R+0.15W")


class RecommendationResult(BaseModel):
    """单条推荐结果。"""

    competition_id: str
    competition_name: str
    recommendation_status: Literal[
        "highly_suitable",   # 85-100 高度适合
        "suitable",          # 70-84 比较适合
        "marginal",          # 55-69 可参加但需补充
        "not_prioritized",   # 0-54 不优先推荐
        "candidate_only",    # 数据待核验，仅展示候选信息，不评分
        "ineligible",        # 硬规则不符合，不进入评分
    ]
    score: Optional[float] = None
    eligible: bool = True
    gate_reasons: list[GateReason] = Field(default_factory=list)
    match_breakdown: Optional[MatchBreakdown] = None
    explanation: dict[str, Any] = Field(default_factory=dict, description="资格/匹配/缺口/队友/时间/下一步")
    urgent: bool = Field(False, description="临近截止标签，不因此提高适配度")
    pending_review: bool = Field(False, description="关键证据仍待补充；不得输出正式评分")


# ---------------------------------------------------------------------------
# 科创机会组合优化
# ---------------------------------------------------------------------------


class PortfolioPreferences(BaseModel):
    """组合规划偏好；只使用完成规划所必需的非敏感信息。"""

    max_competitions: int = Field(3, ge=1, le=4)
    horizon_weeks: int = Field(12, ge=4, le=26)
    weekly_hours_override: Optional[int] = Field(None, ge=1, le=80)
    goal: Literal["award", "growth", "balanced"] = "balanced"


class PortfolioItem(BaseModel):
    competition_id: str
    competition_name: str
    match_score: float
    estimated_total_hours: float
    weekly_load: list[float] = Field(default_factory=list)
    deadline: Optional[date] = None
    reasons: list[str] = Field(default_factory=list)


class RoadmapNode(BaseModel):
    """科创作战地图节点；仅表达可审计的业务状态。"""

    node_id: str
    node_type: Literal["skill", "gap", "competition", "material", "milestone"]
    label: str
    status: Literal["not_ready", "in_progress", "done", "optional", "blocked"] = "not_ready"
    competition_id: Optional[str] = None
    due_date: Optional[date] = None
    reason: Optional[str] = None


class RoadmapEdge(BaseModel):
    source: str
    target: str
    relation: Literal["enables", "requires", "precedes", "impacts"] = "precedes"


class PortfolioPlan(BaseModel):
    mode: Literal["steady", "balanced", "sprint"]
    label: str
    items: list[PortfolioItem] = Field(default_factory=list)
    total_value: float = 0
    capacity_hours: float = 0
    peak_weekly_load: float = 0
    utilization: float = 0
    risk_score: float = 0
    conflicts: list[str] = Field(default_factory=list)
    binding_constraints: list[str] = Field(default_factory=list)
    excluded_reasons: list[str] = Field(default_factory=list)
    solver_method: str = "bounded_exact_enumeration"
    roadmap_nodes: list[RoadmapNode] = Field(default_factory=list)
    roadmap_edges: list[RoadmapEdge] = Field(default_factory=list)


class PortfolioOptimizeResponse(BaseModel):
    generated_at: str
    plans: list[PortfolioPlan] = Field(default_factory=list)


class PortfolioApplyRequest(BaseModel):
    competition_ids: list[str] = Field(..., min_length=1, max_length=4)


# ---------------------------------------------------------------------------
# 动态赛事雷达
# ---------------------------------------------------------------------------


class RadarWatchCreate(BaseModel):
    competition_id: str
    source_url: str
    source_type: Literal["html", "pdf", "docx", "auto"] = "auto"
    css_selector: Optional[str] = None
    include_selector: Optional[str] = Field(None, max_length=300)
    exclude_selector: Optional[str] = Field(None, max_length=500)
    ignore_regex: Optional[str] = Field(None, max_length=1000)
    trigger_terms: list[str] = Field(default_factory=list, max_length=20)
    fetch_mode: Literal["http", "browser_fallback"] = "http"
    timezone: str = Field("Asia/Shanghai", max_length=64)
    interval_hours: int = Field(24, ge=1, le=168)


class RadarEventReview(BaseModel):
    action: Literal["approve", "reject"]
    note: Optional[str] = Field(None, max_length=500)


class RadarAlertAction(BaseModel):
    action: Literal["accept", "ignore", "create_task", "replan"]
    note: Optional[str] = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# 我的项目（只引用赛事，不复制官方截止日期）
# ---------------------------------------------------------------------------


class ProjectItem(BaseModel):
    item_id: int
    item_type: ProjectItemType
    title: str
    due_date: Optional[date] = None
    status: ProjectItemStatus = ProjectItemStatus.TODO
    sort_order: int = 0
    phase: str = "execution"
    depends_on_item_id: Optional[int] = None
    blocked_reason: Optional[str] = None
    source_alert_id: Optional[int] = None
    source_citation_id: Optional[int] = None
    estimated_hours: Optional[float] = Field(None, ge=0)
    is_blocked: bool = False


class ProjectItemCreate(BaseModel):
    item_type: ProjectItemType = ProjectItemType.TASK
    title: str = Field(..., min_length=1, max_length=200)
    due_date: Optional[date] = None
    phase: str = Field("execution", min_length=1, max_length=50)
    depends_on_item_id: Optional[int] = None
    blocked_reason: Optional[str] = Field(None, max_length=500)
    source_alert_id: Optional[int] = None
    source_citation_id: Optional[int] = None
    estimated_hours: Optional[float] = Field(None, ge=0, le=10000)


class ProjectItemUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    due_date: Optional[date] = None
    status: Optional[ProjectItemStatus] = None
    phase: Optional[str] = Field(None, min_length=1, max_length=50)
    depends_on_item_id: Optional[int] = None
    blocked_reason: Optional[str] = Field(None, max_length=500)
    estimated_hours: Optional[float] = Field(None, ge=0, le=10000)


class UserProject(BaseModel):
    project_id: int
    user_id: str
    competition_id: str
    competition_name: str
    document_year: int
    registration_deadline: Optional[date] = None
    submission_deadline: Optional[date] = None
    registration_deadline_at: Optional[datetime] = None
    submission_deadline_at: Optional[datetime] = None
    deadline_timezone_basis: Literal["official", "campus_default"] = "campus_default"
    status: ProjectStatus = ProjectStatus.PLANNED
    created_at: str
    recommendation_ready: bool = True
    readiness_reasons: list[str] = Field(default_factory=list)
    items: list[ProjectItem] = Field(default_factory=list)
    progress_percent: int = Field(0, ge=0, le=100)
    blocked_count: int = Field(0, ge=0)
    risk_level: Literal["low", "medium", "high"] = "low"


class ProjectCreate(BaseModel):
    competition_id: str


class ProjectUpdate(BaseModel):
    status: ProjectStatus


class AgentTaskCreate(BaseModel):
    competition_id: str
    title: str = Field(..., min_length=1, max_length=200)
    due_date: Optional[date] = None
    source_citation_id: Optional[int] = None
    estimated_hours: Optional[float] = Field(None, ge=0, le=10000)


# ---------------------------------------------------------------------------
# 文档解析结果
# ---------------------------------------------------------------------------


class ParsedBlock(BaseModel):
    """PDF/Word 解析后的单段文本块，保留页码与段落序号。"""

    page: int
    paragraph_index: int
    text: str
    competition_id: Optional[str] = None
    rects: list[EvidenceRect] = Field(default_factory=list)
    anchor_quality: Literal["exact", "approximate", "page_only"] = "page_only"


class ParseResult(BaseModel):
    """一次文档解析结果。"""

    document_name: str
    category: CompetitionCategory
    document_year: int
    blocks: list[ParsedBlock] = Field(default_factory=list)
    document_id: Optional[int] = None
    document_sha256: Optional[str] = None
