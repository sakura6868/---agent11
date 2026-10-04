"""真实数据访问层（替换 mock_data）。

默认使用 SQLite（零外部依赖、易部署）；通过环境变量 DATABASE_URL
切换到 PostgreSQL 等生产数据库（SQLAlchemy 统一接口，无需改业务代码）。

数据来源：data/ground_truth/samples/*.json（官方来源锚定数据集）。
seed 为幂等 upsert，重复运行不会重复插入。

提供能力：
  - init_db()            建表 + 首次 seed（赛事 + 演示用户）
  - list_competitions()  列表（按类别/年份过滤）
  - get_competition()    单条 Competition（含 evidence）
  - get_competition_detail()  统一 CompetitionDetail（含 verification）
  - get_user_profile() / save_user_profile()  用户画像读写（真正持久化）
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import (
    DateTime,
    Date,
    ForeignKey,
    Float,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from schemas import (
    AwardDistributionItem,
    Citation,
    Competition,
    CompetitionCategory,
    CompetitionDetail,
    DataStatus,
    EducationLevel,
    FactTag,
    Grade,
    RequirementItem,
    SourceItem,
    TimelineItem,
    TrustedLevel,
    UserProfile,
    UserProject,
    ProjectItem,
    ProjectItemStatus,
    ProjectItemType,
    ProjectStatus,
    Verification,
)
from fact_formatting import format_team_size
from trust import assess_recommendation_readiness, assess_source_readiness, is_registerable_now
from contest_clock import contest_now
from recommendation.engine import eligibility_gate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "ground_truth" / "samples"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "campus_agent.db"

# Render / 部分云厂商注入的 DATABASE_URL 使用 postgres:// 协议头，
# 而 SQLAlchemy 2.0 只认 postgresql://，需在此统一归一化，否则连库失败。
_DATABASE_URL_RAW = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH.as_posix()}")
if _DATABASE_URL_RAW.startswith("postgres://"):
    DATABASE_URL = _DATABASE_URL_RAW.replace("postgres://", "postgresql://", 1)
elif _DATABASE_URL_RAW.startswith("sqlite:///"):
    # 相对路径（如 .env 中的 ./data/campus_agent.db）必须相对于项目根解析，
    # 否则从不同工作目录启动（src/ 或项目根）会落到两个不同的库文件。
    _rel = _DATABASE_URL_RAW[len("sqlite:///"):]
    if not os.path.isabs(_rel):
        _rel = (PROJECT_ROOT / _rel).resolve().as_posix()
    DATABASE_URL = f"sqlite:///{_rel}"
else:
    DATABASE_URL = _DATABASE_URL_RAW


# ---------------------------------------------------------------------------
# ORM 模型
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class CompetitionModel(Base):
    __tablename__ = "competitions"

    competition_id: Mapped[str] = mapped_column(String, primary_key=True)
    competition_name: Mapped[str] = mapped_column(String, nullable=False)
    document_year: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    organizer: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # 资格相关
    eligible_students: Mapped[list] = mapped_column(String, nullable=False, default="[]")  # JSON list[str]
    allowed_grades: Mapped[Optional[list]] = mapped_column(String, nullable=True)  # JSON list[str] | None
    allowed_majors: Mapped[Optional[list]] = mapped_column(String, nullable=True)  # JSON list[str] | None
    team_required: Mapped[bool] = mapped_column(default=False)
    team_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    team_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 时间相关
    registration_deadline: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    submission_deadline: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    registration_deadline_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    submission_deadline_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    deadline_timezone_basis: Mapped[str] = mapped_column(String, default="campus_default")
    result_announcement_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    competition_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    competition_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # 奖项设置
    award_settings: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    award_distribution: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # JSON list[AwardDistributionItem]
    brief_description: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 比赛简要说明

    # 材料与能力
    required_materials: Mapped[list] = mapped_column(String, nullable=False, default="[]")
    evaluation_dimensions: Mapped[list] = mapped_column(String, nullable=False, default="[]")
    required_skills: Mapped[list] = mapped_column(String, nullable=False, default="[]")

    # 来源与可信
    official_source_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_acquired_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    trusted_level: Mapped[str] = mapped_column(String, default="C")
    data_status: Mapped[str] = mapped_column(String, default="unverified")
    last_verified_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # 官方来源核实结论：found（已找到官方来源）/ not_found（未找到官方链接）
    official_source_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    doc_version: Mapped[str] = mapped_column(String, default="1.0")

    citations: Mapped[list["CitationModel"]] = relationship(
        back_populates="competition",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class CitationModel(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[str] = mapped_column(
        String, ForeignKey("competitions.competition_id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[str] = mapped_column(String, nullable=False, default="")
    page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_text: Mapped[str] = mapped_column(String, nullable=False, default="")
    document_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    acquired_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_verified_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    trusted_level: Mapped[str] = mapped_column(String, default="C")
    document_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    document_sha256: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    page_rects: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    anchor_quality: Mapped[str] = mapped_column(String, nullable=False, default="page_only")
    text_exact: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_suffix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    text_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    anchor_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    anchor_status: Mapped[str] = mapped_column(String, nullable=False, default="original")

    competition: Mapped["CompetitionModel"] = relationship(back_populates="citations")


class DocumentModel(Base):
    """不可变官方文档；内容按 SHA-256 去重，确保引用对应历史版本。"""

    __tablename__ = "documents"

    document_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class SourceWatchModel(Base):
    __tablename__ = "source_watches"

    watch_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[str] = mapped_column(
        String, ForeignKey("competitions.competition_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="auto")
    css_selector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    include_selector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    exclude_selector: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ignore_regex: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_terms: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    fetch_mode: Mapped[str] = mapped_column(String, nullable=False, default="http")
    timezone_name: Mapped[str] = mapped_column(String, nullable=False, default="Asia/Shanghai")
    interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    etag: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_modified: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_content_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_status: Mapped[str] = mapped_column(String, nullable=False, default="new")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_content_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class SourceSnapshotModel(Base):
    __tablename__ = "source_snapshots"

    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("source_watches.watch_id", ondelete="CASCADE"), nullable=False, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_etag: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    response_last_modified: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class SourceChangeEventModel(Base):
    __tablename__ = "source_change_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("source_watches.watch_id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_snapshot_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    new_snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    severity: Mapped[str] = mapped_column(String, nullable=False, default="low")
    change_type: Mapped[str] = mapped_column(String, nullable=False, default="content_changed")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    diff_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    affected_fields: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    field_changes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    proposed_changes: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class UserAlertModel(Base):
    __tablename__ = "user_alerts"

    alert_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    competition_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    action_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class UserProfileModel(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    education_level: Mapped[str] = mapped_column(String, nullable=False)
    grade: Mapped[str] = mapped_column(String, nullable=False)
    major: Mapped[str] = mapped_column(String, nullable=False)
    skills: Mapped[list] = mapped_column(String, nullable=False, default="[]")
    experiences: Mapped[list] = mapped_column(String, nullable=False, default="[]")
    weekly_available_hours: Mapped[int] = mapped_column(Integer, default=10)
    expected_team_size: Mapped[int] = mapped_column(Integer, default=1)
    privacy_consent: Mapped[bool] = mapped_column(default=False)

    # 登录/展示用（可选）
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    persona: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    avatar: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AuthUser(Base):
    """登录账号（与画像分离：用户名 + 密码哈希 + 是否测试账号）。"""

    __tablename__ = "auth_users"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_test: Mapped[bool] = mapped_column(default=False)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class AgentRunModel(Base):
    """可回放的业务运行摘要；不保存提示词、模型私有思维链或敏感凭证。"""

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_competition: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="completed")
    total_duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


# —— 密码哈希（仅用标准库，零外部依赖）——
def _hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000)
    return f"pbkdf2_sha256$100000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储的 pbkdf2 哈希是否一致（恒定时间比较）。"""
    try:
        algo, iters, salt, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def get_auth_user(username: str):
    with session_scope() as session:
        m = session.get(AuthUser, username)
        if m is None:
            return None
        return {
            "username": m.username,
            "is_test": m.is_test,
            "display_name": m.display_name,
            "password_hash": m.password_hash,
        }


def create_auth_user(
    username: str, password: str, is_test: bool = False, display_name: Optional[str] = None
) -> bool:
    """创建登录账号；用户名已存在返回 False。"""
    with session_scope() as session:
        if session.get(AuthUser, username) is not None:
            return False
        session.add(
            AuthUser(
                username=username,
                password_hash=_hash_password(password),
                is_test=is_test,
                display_name=display_name,
            )
        )
        return True


def save_agent_run(result: dict) -> str:
    """保存一次可审计运行摘要，返回 run_id；失败不阻断问答主链。"""
    run_id = str(result.get("run_id") or secrets.token_hex(12))
    summary = {
        "trace": result.get("trace") or [],
        "trace_summary": result.get("trace_summary") or {},
        "metrics": result.get("metrics") or {},
        "pending_review": bool(result.get("pending_review")),
        "citation_ids": [item.get("citation_id") for item in (result.get("citations") or []) if item.get("citation_id")],
        "data_version": result.get("metrics", {}).get("data_version") if isinstance(result.get("metrics"), dict) else None,
        "planning": result.get("planning"),
    }
    try:
        with session_scope() as session:
            existing = session.get(AgentRunModel, run_id)
            if existing is None:
                existing = AgentRunModel(run_id=run_id)
                session.add(existing)
            existing.user_id = result.get("user_id")
            existing.question = str(result.get("question") or "")[:4000]
            existing.intent = result.get("intent")
            existing.resolved_competition = result.get("resolved_competition")
            existing.status = "review_required" if result.get("pending_review") else "completed"
            existing.total_duration_ms = (result.get("metrics") or {}).get("total_duration_ms")
            existing.summary_json = _dump_json(summary)
        return run_id
    except Exception:
        return run_id


def list_agent_runs(user_id: Optional[str] = None, limit: int = 30) -> list[dict]:
    with session_scope() as session:
        query = session.query(AgentRunModel)
        if user_id:
            query = query.filter(AgentRunModel.user_id == user_id)
        rows = query.order_by(AgentRunModel.created_at.desc()).limit(limit).all()
        return [
            {
                "run_id": row.run_id,
                "user_id": row.user_id,
                "question": row.question,
                "intent": row.intent,
                "resolved_competition": row.resolved_competition,
                "status": row.status,
                "total_duration_ms": row.total_duration_ms,
                "created_at": row.created_at.isoformat(),
                **(_load_json(row.summary_json) if row.summary_json else {}),
            }
            for row in rows
        ]


def get_agent_run(run_id: str) -> Optional[dict]:
    with session_scope() as session:
        row = session.get(AgentRunModel, run_id)
        if row is None:
            return None
        return {
            "run_id": row.run_id,
            "user_id": row.user_id,
            "question": row.question,
            "intent": row.intent,
            "resolved_competition": row.resolved_competition,
            "status": row.status,
            "total_duration_ms": row.total_duration_ms,
            "created_at": row.created_at.isoformat(),
            **(_load_json(row.summary_json) if row.summary_json else {}),
        }


# 演示测试账号（硬编码用户名；如需改密码改这里即可）
TEST_ACCOUNT_USERNAME = "test"
TEST_ACCOUNT_PASSWORD = "test123"


def seed_test_account(session) -> None:
    """幂等 seed 测试账号及其默认画像（便于登录即见推荐 / 千人千面）。"""
    au = session.get(AuthUser, TEST_ACCOUNT_USERNAME)
    if au is None:
        session.add(
            AuthUser(
                username=TEST_ACCOUNT_USERNAME,
                password_hash=_hash_password(TEST_ACCOUNT_PASSWORD),
                is_test=True,
                display_name="测试账号",
            )
        )
    prof = session.get(UserProfileModel, TEST_ACCOUNT_USERNAME)
    if prof is None:
        session.add(
            UserProfileModel(
                user_id=TEST_ACCOUNT_USERNAME,
                education_level=EducationLevel.UNDERGRADUATE.value,
                grade=Grade.SOPHOMORE.value,
                major="计算机科学与技术",
                skills=_dump_json(["Python", "算法", "前端开发"]),
                experiences=_dump_json([]),
                weekly_available_hours=12,
                expected_team_size=3,
                privacy_consent=True,
                display_name="测试账号",
                persona="演示账号 · 可切换学生类型",
                avatar="🧪",
            )
        )


class UserProjectModel(Base):
    __tablename__ = "user_projects"
    __table_args__ = (
        Index(
            "ux_user_projects_user_competition",
            "user_id",
            "competition_id",
            unique=True,
        ),
    )

    project_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competition_id: Mapped[str] = mapped_column(
        String, ForeignKey("competitions.competition_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="planned")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    competition: Mapped["CompetitionModel"] = relationship(lazy="joined")
    items: Mapped[list["ProjectItemModel"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )


class ProjectItemModel(Base):
    __tablename__ = "project_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_type: Mapped[str] = mapped_column(String, nullable=False, default="task")
    title: Mapped[str] = mapped_column(String, nullable=False)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="todo")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phase: Mapped[str] = mapped_column(String, nullable=False, default="execution")
    depends_on_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_alert_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_citation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    project: Mapped["UserProjectModel"] = relationship(back_populates="items")


# ---------------------------------------------------------------------------
# 引擎与会话
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
        _engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


_initialized = False


def init_db() -> None:
    """建表并首次 seed（幂等，可重复调用）。可在应用启动时或首次 DB 访问时触发。"""
    global _initialized
    if _initialized:
        return
    get_engine()
    Base.metadata.create_all(_engine)
    _migrate_user_profile_columns()
    _migrate_competition_columns()
    _migrate_citation_columns()
    _migrate_innovation_columns()
    _migrate_auth_user_columns()
    _migrate_user_project_unique_index()
    # 必须在 seed 之前置位，避免 seed -> session_scope -> init_db 递归
    _initialized = True
    seed_all()


def _add_missing_columns(table, url: str) -> None:
    """幂等增量迁移：为已存在的表补齐 ORM 模型声明但库表缺失的列（跨 SQLite / PostgreSQL）。

    SQLAlchemy 的 create_all 不会向已存在的表添加新列，因此这里根据模型元数据
    检查缺失列并 ALTER TABLE 补齐。列集合直接从 ORM 模型派生，**新增字段时无需
    再手工维护白名单**，从根本上避免「本地有、线上缺列」的 schema 漂移问题。
    """
    from sqlalchemy import text

    dialect = _engine.dialect
    wanted = {c.name: c for c in table.columns}
    with _engine.begin() as conn:
        if url.startswith("sqlite"):
            rows = conn.execute(text(f"PRAGMA table_info({table.name})")).fetchall()
            existing = {r[1] for r in rows}
        else:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{table.name}'"
                )
            ).fetchall()
            existing = {r[0] for r in rows}
        for name, col in wanted.items():
            if name in existing:
                continue
            col_type = col.type.compile(dialect=dialect)
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {name} {col_type}"
            # 已有数据表上不能直接添加 NOT NULL 列；若存在标量默认值则补 DEFAULT 过渡。
            if (
                (not col.nullable)
                and col.default is not None
                and not callable(getattr(col.default, "arg", None))
            ):
                lit = col.default.arg
                if isinstance(lit, str):
                    ddl += f" DEFAULT '{lit.replace(chr(39), chr(39) * 2)}'"
                else:
                    ddl += f" DEFAULT {lit}"
            conn.execute(text(ddl))


def _migrate_competition_columns() -> None:
    """为 competitions 表补齐模型声明但库表缺失的列。"""
    _add_missing_columns(CompetitionModel.__table__, DATABASE_URL)


def _migrate_citation_columns() -> None:
    """为历史 citations 表补齐证据坐标与不可变文档字段。"""
    _add_missing_columns(CitationModel.__table__, DATABASE_URL)


def _migrate_innovation_columns() -> None:
    """为雷达控制塔、收件箱和执行任务补齐新增列。"""
    for table in (
        SourceWatchModel.__table__,
        SourceChangeEventModel.__table__,
        UserAlertModel.__table__,
        ProjectItemModel.__table__,
    ):
        _add_missing_columns(table, DATABASE_URL)


def _migrate_user_profile_columns() -> None:
    """为 user_profiles 表补齐模型声明但库表缺失的列（跨 dialect）。"""
    _add_missing_columns(UserProfileModel.__table__, DATABASE_URL)


def _migrate_auth_user_columns() -> None:
    """为 auth_users 表补齐模型声明但库表缺失的列（跨 dialect）。"""
    _add_missing_columns(AuthUser.__table__, DATABASE_URL)


def _migrate_user_project_unique_index() -> None:
    """为旧库补上项目幂等约束；发现历史重复数据时保守跳过。"""
    from sqlalchemy import text

    with _engine.begin() as conn:
        duplicate = conn.execute(
            text(
                "SELECT user_id, competition_id FROM user_projects "
                "GROUP BY user_id, competition_id HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is None:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_projects_user_competition "
                    "ON user_projects (user_id, competition_id)"
                )
            )


@contextmanager
def session_scope() -> Iterator[object]:
    if not _initialized:
        init_db()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# JSON 列辅助（SQLite 存文本，PostgreSQL 可改 JSON 类型）
# ---------------------------------------------------------------------------


def _dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(text: Optional[str]):
    if text is None or text == "":
        return []
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []


def _to_date(v: Optional[str]) -> Optional[date]:
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except (ValueError, TypeError):
        return None


def _to_enum(cls, v, default=None):
    try:
        return cls(v)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 转换：ORM -> Pydantic
# ---------------------------------------------------------------------------


def _citation_to_pydantic(m: CitationModel) -> Citation:
    rects = _load_json(m.page_rects)
    confidence = float(m.anchor_confidence or 0.0)
    if rects and m.anchor_quality == "exact":
        confidence = max(confidence, 0.95)
    elif rects and m.anchor_quality == "approximate":
        confidence = max(confidence, 0.72)
    elif m.page:
        confidence = max(confidence, 0.45)
    return Citation(
        citation_id=m.id,
        field=m.field,
        page=m.page,
        source_text=m.source_text,
        document_name=m.document_name,
        source_url=m.source_url,
        acquired_date=m.acquired_date,
        last_verified_at=m.last_verified_at,
        trusted_level=_to_enum(TrustedLevel, m.trusted_level, TrustedLevel.C),
        document_id=m.document_id,
        document_sha256=m.document_sha256,
        rects=rects,
        anchor_quality=m.anchor_quality or "page_only",
        text_exact=m.text_exact or m.source_text or None,
        text_prefix=m.text_prefix,
        text_suffix=m.text_suffix,
        text_start=m.text_start,
        text_end=m.text_end,
        anchor_confidence=confidence,
        anchor_status=m.anchor_status or "original",
    )


def _competition_to_pydantic(m: CompetitionModel) -> Competition:
    allowed_grades = None
    if m.allowed_grades is not None:
        raw = _load_json(m.allowed_grades)
        allowed_grades = [_to_enum(Grade, g, Grade.FRESHMAN) for g in raw]
    allowed_majors = _load_json(m.allowed_majors) if m.allowed_majors is not None else None
    return Competition(
        competition_id=m.competition_id,
        competition_name=m.competition_name,
        document_year=m.document_year,
        category=_to_enum(CompetitionCategory, m.category, CompetitionCategory.PROGRAMMING),
        organizer=m.organizer,
        # 学历属于资格门控字段，未知值必须显式失败，不能静默降级成本科生。
        eligible_students=[EducationLevel(s) for s in _load_json(m.eligible_students)],
        allowed_grades=allowed_grades,
        allowed_majors=allowed_majors,
        team_required=m.team_required,
        team_min=m.team_min,
        team_max=m.team_max,
        registration_deadline=m.registration_deadline,
        submission_deadline=m.submission_deadline,
        registration_deadline_at=m.registration_deadline_at,
        submission_deadline_at=m.submission_deadline_at,
        deadline_timezone_basis=m.deadline_timezone_basis or "campus_default",
        result_announcement_date=m.result_announcement_date,
        competition_start_date=m.competition_start_date,
        competition_end_date=m.competition_end_date,
        award_settings=m.award_settings,
        award_distribution=[AwardDistributionItem(**a) for a in _load_json(m.award_distribution)],
        brief_description=m.brief_description,
        required_materials=_load_json(m.required_materials),
        evaluation_dimensions=_load_json(m.evaluation_dimensions),
        required_skills=_load_json(m.required_skills),
        official_source_url=m.official_source_url,
        source_acquired_date=m.source_acquired_date,
        trusted_level=_to_enum(TrustedLevel, m.trusted_level, TrustedLevel.C),
        data_status=_to_enum(DataStatus, m.data_status, DataStatus.UNVERIFIED),
        last_verified_at=m.last_verified_at,
        official_source_status=m.official_source_status,
        notes=m.notes,
        doc_version=m.doc_version,
        evidence=[_citation_to_pydantic(c) for c in m.citations],
    )


# ---------------------------------------------------------------------------
# Seed（幂等）
# ---------------------------------------------------------------------------

DEMO_USER = UserProfile(
    user_id="mock_user_001",
    education_level=EducationLevel.UNDERGRADUATE,
    grade=Grade.SOPHOMORE,
    major="计算机科学与技术",
    skills=["Python", "C/C++", "算法与数据结构", "Java", "Git", "前端开发", "后端开发", "数据库", "机器学习", "Web开发"],
    experiences=["蓝桥杯省赛", "全国大学生程序设计竞赛", "中国软件杯", "校园黑客松获奖"],
    weekly_available_hours=12,
    expected_team_size=3,
    privacy_consent=True,
    display_name="示例用户",
    persona="综合型 · 计算机大二",
    avatar="🙂",
)

# 特色鲜明的示例用户：登录后画像/推荐/问答结果各不相同，直观体现「千人千面」
DEMO_USERS: list[UserProfile] = [
    DEMO_USER,
    UserProfile(
        user_id="stu_algo",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.JUNIOR,
        major="计算机科学与技术",
        skills=["C++", "算法", "数据结构", "动态规划", "Python"],
        experiences=["蓝桥杯省一等奖", "ACM-ICPC 区域赛"],
        weekly_available_hours=22,
        expected_team_size=1,
        privacy_consent=True,
        display_name="算法卷王",
        persona="算法竞赛型 · 偏好个人赛，冲省一冲金牌",
        avatar="👨‍💻",
    ),
    UserProfile(
        user_id="stu_model",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SOPHOMORE,
        major="信息与计算科学",
        skills=["MATLAB", "数学建模", "论文写作", "Python", "数据分析"],
        experiences=["数学建模校赛二等奖"],
        weekly_available_hours=15,
        expected_team_size=3,
        privacy_consent=True,
        display_name="建模选手",
        persona="数学建模型 · 三人队，重论文与数据",
        avatar="📐",
    ),
    UserProfile(
        user_id="stu_maker",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SENIOR,
        major="工商管理",
        skills=["商业计划书", "路演演讲", "项目管理", "市场调研"],
        experiences=["互联网+ 校赛", "大创项目负责人"],
        weekly_available_hours=10,
        expected_team_size=4,
        privacy_consent=True,
        display_name="创业先锋",
        persona="创新创业型 · 组大队，主打商业落地",
        avatar="🚀",
    ),
    UserProfile(
        user_id="stu_dev",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.JUNIOR,
        major="软件工程",
        skills=["Java", "Spring", "前端", "数据库", "Git"],
        experiences=["中国软件杯"],
        weekly_available_hours=18,
        expected_team_size=3,
        privacy_consent=True,
        display_name="全栈开发",
        persona="软件作品型 · 三人队，做完整产品",
        avatar="🛠️",
    ),
    UserProfile(
        user_id="stu_rookie",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.FRESHMAN,
        major="电子信息工程",
        skills=["C语言"],
        experiences=[],
        weekly_available_hours=8,
        expected_team_size=1,
        privacy_consent=True,
        display_name="科创萌新",
        persona="入门探索型 · 大一，找门槛低的比赛练手",
        avatar="🌱",
    ),
    UserProfile(
        user_id="stu_grad",
        education_level=EducationLevel.POSTGRADUATE,
        grade=Grade.MASTER_FIRST,
        major="人工智能",
        skills=["深度学习", "PyTorch", "论文写作", "Python", "NLP"],
        experiences=["发表 SCI 论文", "研究生数学建模竞赛"],
        weekly_available_hours=20,
        expected_team_size=3,
        privacy_consent=True,
        display_name="AI研究生",
        persona="研究生 · AI方向，偏研究型高含金量赛事",
        avatar="🧠",
    ),
    # —— 以下为「队友推荐」候选池补充：补齐计算机全栈选手常缺的设计/可视化/硬件能力 ——
    UserProfile(
        user_id="mate_ui",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.JUNIOR,
        major="视觉传达设计",
        skills=["UI设计", "Figma", "Photoshop", "海报设计", "品牌视觉", "交互设计"],
        experiences=["大广赛", "中国大学生计算机设计大赛"],
        weekly_available_hours=14,
        expected_team_size=3,
        privacy_consent=True,
        display_name="设计美学",
        persona="设计美学型 · 负责界面与视觉呈现",
        avatar="🎨",
    ),
    UserProfile(
        user_id="mate_viz",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SOPHOMORE,
        major="数据科学与大数据技术",
        skills=["数据可视化", "Tableau", "PPT汇报", "Python", "统计分析", "Excel"],
        experiences=["正大杯市场调研", "数据可视化校赛一等奖"],
        weekly_available_hours=13,
        expected_team_size=3,
        privacy_consent=True,
        display_name="数据讲述者",
        persona="数据讲述型 · 负责图表与答辩汇报",
        avatar="📊",
    ),
    UserProfile(
        user_id="mate_hw",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.JUNIOR,
        major="自动化",
        skills=["嵌入式", "STM32", "硬件电路", "C语言", "物联网", "传感器"],
        experiences=["智能车竞赛", "电子设计大赛省赛"],
        weekly_available_hours=16,
        expected_team_size=4,
        privacy_consent=True,
        display_name="硬核工程",
        persona="硬核工程型 · 负责硬件与嵌入式实现",
        avatar="⚙️",
    ),
]


def _upsert_competition_from_raw(raw: dict, session) -> None:
    cid = raw.get("competition_id") or Path(raw.get("_source_file", "")).stem
    if not cid:
        return
    if raw.get("registration_deadline_at") or raw.get("submission_deadline_at"):
        Competition.model_validate({**raw, "competition_id": cid})
    cols = {
        "competition_id": cid,
        "competition_name": raw["competition_name"],
        "document_year": int(raw["document_year"]),
        "category": raw["category"],
        "organizer": raw.get("organizer"),
        "eligible_students": _dump_json(raw.get("eligible_students", [])),
        "allowed_grades": None if raw.get("allowed_grades") is None else _dump_json(raw["allowed_grades"]),
        "allowed_majors": None if raw.get("allowed_majors") is None else _dump_json(raw["allowed_majors"]),
        "team_required": bool(raw.get("team_required", False)),
        "team_min": raw.get("team_min"),
        "team_max": raw.get("team_max"),
        "registration_deadline": _to_date(raw.get("registration_deadline")),
        "submission_deadline": _to_date(raw.get("submission_deadline")),
        "registration_deadline_at": raw.get("registration_deadline_at"),
        "submission_deadline_at": raw.get("submission_deadline_at"),
        "deadline_timezone_basis": raw.get("deadline_timezone_basis") or "campus_default",
        "result_announcement_date": _to_date(raw.get("result_announcement_date")),
        "competition_start_date": _to_date(raw.get("competition_start_date")),
        "competition_end_date": _to_date(raw.get("competition_end_date")),
        "award_settings": raw.get("award_settings"),
        "award_distribution": _dump_json(raw.get("award_distribution") or []),
        "brief_description": raw.get("brief_description"),
        "required_materials": _dump_json(raw.get("required_materials") or []),
        "evaluation_dimensions": _dump_json(raw.get("evaluation_dimensions") or []),
        "required_skills": _dump_json(raw.get("required_skills") or []),
        "official_source_url": raw.get("official_source_url"),
        "source_acquired_date": raw.get("source_acquired_date"),
        "trusted_level": raw.get("trusted_level") or "C",
        "data_status": raw.get("data_status") or "unverified",
        "last_verified_at": raw.get("last_verified_at"),
        "official_source_status": raw.get("official_source_status"),
        "notes": raw.get("notes"),
        "doc_version": raw.get("doc_version") or f"{raw.get('document_year')}_v1",
    }

    # upsert 主表
    existing = session.get(CompetitionModel, cid)
    if existing is None:
        existing = CompetitionModel(competition_id=cid)
        session.add(existing)
    for k, v in cols.items():
        setattr(existing, k, v)

    # 证据按稳定内容键原位更新：既与最新 JSON 保持一致，也保留雷达运行后
    # 补充的 citation_id、不可变文档指纹和 X 光坐标，避免应用重启后丢失。
    old_citations = list(existing.citations)
    old_by_key = {
        (item.field, item.page, item.source_text, item.source_url): item
        for item in old_citations
    }
    retained_ids: set[int] = set()
    for e in raw.get("evidence", []):
        key = (
            e.get("field", ""),
            e.get("page"),
            e.get("source_text", ""),
            e.get("source_url"),
        )
        citation = old_by_key.get(key)
        if citation is None:
            citation = CitationModel(competition_id=cid)
            session.add(citation)
        elif citation.id is not None:
            retained_ids.add(citation.id)

        citation.field = e.get("field", "")
        citation.page = e.get("page")
        citation.source_text = e.get("source_text", "")
        citation.document_name = e.get("document_name")
        citation.source_url = e.get("source_url")
        citation.acquired_date = e.get("acquired_date")
        citation.last_verified_at = e.get("last_verified_at")
        citation.trusted_level = e.get("trusted_level") or "C"
        # ground-truth 文件通常不含运行时定位字段；只有显式提供非空值时才覆盖。
        if e.get("document_id") is not None:
            citation.document_id = e["document_id"]
        if e.get("document_sha256"):
            citation.document_sha256 = e["document_sha256"]
        if e.get("rects"):
            citation.page_rects = _dump_json(e["rects"])
        elif citation.page_rects is None:
            citation.page_rects = "[]"
        if e.get("anchor_quality"):
            citation.anchor_quality = e["anchor_quality"]
        elif not citation.anchor_quality:
            citation.anchor_quality = "page_only"
        citation.text_exact = e.get("text_exact") or citation.text_exact or citation.source_text
        if e.get("text_prefix") is not None:
            citation.text_prefix = e.get("text_prefix")
        if e.get("text_suffix") is not None:
            citation.text_suffix = e.get("text_suffix")
        if e.get("text_start") is not None:
            citation.text_start = e.get("text_start")
        if e.get("text_end") is not None:
            citation.text_end = e.get("text_end")
        if e.get("anchor_confidence") is not None:
            citation.anchor_confidence = float(e.get("anchor_confidence"))
        elif not citation.anchor_confidence:
            citation.anchor_confidence = 1.0 if citation.anchor_quality == "exact" else 0.45
        if e.get("anchor_status"):
            citation.anchor_status = e.get("anchor_status")
        elif not citation.anchor_status:
            citation.anchor_status = "original"

    for old in old_citations:
        if old.id not in retained_ids:
            session.delete(old)


def seed_competitions(session) -> int:
    """从 ground_truth/samples 灌库，返回灌入条数。"""
    if not GROUND_TRUTH_DIR.exists():
        return 0
    count = 0
    for f in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        raw.setdefault("competition_id", f.stem)
        raw["_source_file"] = str(f)
        _upsert_competition_from_raw(raw, session)
        count += 1
    return count


def seed_demo_user(session) -> None:
    """幂等 seed 多个特色用户；已存在则补齐缺失的展示字段（旧库迁移后回填）。"""
    for u in DEMO_USERS:
        existing = session.get(UserProfileModel, u.user_id)
        if existing is None:
            session.add(
                UserProfileModel(
                    user_id=u.user_id,
                    education_level=u.education_level.value,
                    grade=u.grade.value,
                    major=u.major,
                    skills=_dump_json(u.skills),
                    experiences=_dump_json(u.experiences),
                    weekly_available_hours=u.weekly_available_hours,
                    expected_team_size=u.expected_team_size,
                    privacy_consent=u.privacy_consent,
                    display_name=u.display_name,
                    persona=u.persona,
                    avatar=u.avatar,
                )
            )
        else:
            # 旧库迁移：仅当展示字段为空时回填（不覆盖用户已手动修改的画像）
            if not existing.display_name and u.display_name:
                existing.display_name = u.display_name
            if not existing.persona and u.persona:
                existing.persona = u.persona
            if not existing.avatar and u.avatar:
                existing.avatar = u.avatar


def seed_source_watches(session) -> None:
    """为已核验赛事创建少量幂等监控项；首次扫描只建立基线。"""
    rows = (
        session.query(CompetitionModel)
        .filter(
            CompetitionModel.data_status == DataStatus.VERIFIED.value,
            CompetitionModel.trusted_level == TrustedLevel.A.value,
            CompetitionModel.official_source_status == "found",
            CompetitionModel.official_source_url.is_not(None),
        )
        .order_by(CompetitionModel.competition_id)
        .limit(12)
        .all()
    )
    for comp in rows:
        exists = (
            session.query(SourceWatchModel)
            .filter(
                SourceWatchModel.competition_id == comp.competition_id,
                SourceWatchModel.source_url == comp.official_source_url,
            )
            .first()
        )
        if exists is None:
            source_type = "pdf" if ".pdf" in (comp.official_source_url or "").lower() else "auto"
            session.add(
                SourceWatchModel(
                    competition_id=comp.competition_id,
                    source_url=comp.official_source_url,
                    source_type=source_type,
                    interval_hours=24,
                )
            )


def seed_demo_project(session) -> None:
    """为演示账号幂等准备一个七阶段项目样例。

    全新 clone 后直接启动，演示路径的「我的项目 / 七阶段工作台」即可直接展示内容，
    无需现场先创建项目。

    情报收件箱**不预置伪造变更**——那会违背本项目「不编造、来源可追溯」的核心原则。
    演示第 2 步应通过管理界面「演示截止日期变化」现场真实生成，系统会自动把情报
    推送到本项目并标记受影响阶段，演示效果真实且可复核。
    """
    user_id = TEST_ACCOUNT_USERNAME
    exists = session.query(UserProjectModel).filter(UserProjectModel.user_id == user_id).first()
    if exists is not None:
        return
    candidates = (
        session.query(CompetitionModel)
        .filter(
            CompetitionModel.data_status == DataStatus.VERIFIED.value,
            CompetitionModel.trusted_level == TrustedLevel.A.value,
            CompetitionModel.official_source_status == "found",
            CompetitionModel.registration_deadline.is_not(None),
        )
        .order_by(CompetitionModel.competition_id)
        .all()
    )
    for comp in candidates:
        if assess_recommendation_readiness(_competition_to_pydantic(comp), contest_now()).ready:
            _get_or_create_project_model(session, user_id, comp)
            return


def seed_all() -> int:
    """幂等 seed：赛事 Ground Truth + 演示用户 + 测试账号 + 演示项目。重复运行安全。"""
    with session_scope() as session:
        n = seed_competitions(session)
        seed_demo_user(session)
        seed_test_account(session)
        seed_source_watches(session)
        seed_demo_project(session)
    return n


# ---------------------------------------------------------------------------
# 仓储查询
# ---------------------------------------------------------------------------


def list_competitions(
    category: Optional[str] = None, year: Optional[int] = None
) -> list[Competition]:
    with session_scope() as session:
        query = session.query(CompetitionModel)
        if category:
            query = query.filter(CompetitionModel.category == category)
        if year is not None:
            query = query.filter(CompetitionModel.document_year == year)
        rows = query.order_by(CompetitionModel.document_year.desc()).all()
        # 在 session 内完成转换（避免 detached 对象）
        return [_competition_to_pydantic(m) for m in rows]


def get_competition(competition_id: str) -> Optional[Competition]:
    with session_scope() as session:
        m = session.get(CompetitionModel, competition_id)
        if m is None:
            return None
        return _competition_to_pydantic(m)


def get_all_competitions() -> list[Competition]:
    with session_scope() as session:
        rows = session.query(CompetitionModel).all()
        return [_competition_to_pydantic(m) for m in rows]


def get_competition_detail(competition_id: str) -> Optional[CompetitionDetail]:
    comp = get_competition(competition_id)
    if comp is None:
        return None

    timeline = []
    if comp.result_announcement_date:
        timeline.append(TimelineItem(label="成绩公布", event_date=comp.result_announcement_date))
    comp_range = None
    if comp.competition_start_date and comp.competition_end_date:
        comp_range = f"{comp.competition_start_date} 至 {comp.competition_end_date}"
    elif comp.competition_start_date:
        comp_range = f"{comp.competition_start_date} 起"
    elif comp.competition_end_date:
        comp_range = f"至 {comp.competition_end_date}"
    if comp_range:
        timeline.append(TimelineItem(label="比赛时间", date_text=comp_range))
    if comp.award_settings:
        timeline.append(TimelineItem(label="奖项设置", date_text=comp.award_settings))
    if not timeline:
        timeline.append(TimelineItem(label="暂无后续安排", date_text="以官方通知为准"))

    requirements = [
        RequirementItem(
            tag=FactTag.OFFICIAL,
            text=f"团队人数：{format_team_size(comp.team_min, comp.team_max)}"
            + ("（个人赛）" if not comp.team_required else "（组队赛）"),
        ),
        RequirementItem(
            tag=FactTag.OFFICIAL,
            text=f"所需材料：{', '.join(comp.required_materials) or '未明确'}",
        ),
    ]

    sources = [
        SourceItem(
            name=f"{comp.competition_name}_{comp.document_year}_官方通知",
            url=comp.official_source_url,
            acquired_date=comp.source_acquired_date,
            trusted_level=comp.trusted_level,
        )
    ]

    readiness = assess_recommendation_readiness(comp, contest_now())
    verification = Verification(
        status=comp.data_status,
        last_verified_at=comp.last_verified_at,
        trusted_level=comp.trusted_level,
        note=(
            "官网来源已确认，关键字段具备可追溯证据；请以官网最新通知为准"
            if assess_source_readiness(comp).ready
            else "关键证据待补充，不用于正式资格判断或匹配评分"
        ),
    )

    return CompetitionDetail(
        competition=comp,
        timeline=timeline,
        requirements=requirements,
        sources=sources,
        verification=verification,
        recommendation_ready=readiness.ready,
        readiness_reasons=list(readiness.reasons),
    )


def _user_to_pydantic(m: UserProfileModel) -> UserProfile:
    return UserProfile(
        user_id=m.user_id,
        education_level=EducationLevel(m.education_level),
        grade=_to_enum(Grade, m.grade, Grade.UNKNOWN),
        major=m.major,
        skills=_load_json(m.skills),
        experiences=_load_json(m.experiences),
        weekly_available_hours=m.weekly_available_hours,
        expected_team_size=m.expected_team_size,
        privacy_consent=m.privacy_consent,
        display_name=getattr(m, "display_name", None),
        persona=getattr(m, "persona", None),
        avatar=getattr(m, "avatar", None),
    )


def get_user_profile(user_id: str) -> Optional[UserProfile]:
    with session_scope() as session:
        m = session.get(UserProfileModel, user_id)
        if m is None:
            return None
        return _user_to_pydantic(m)


def list_demo_profiles(exclude_user_id: Optional[str] = None) -> list[UserProfile]:
    """Public demonstrations use immutable fictitious fixtures, never registered profiles."""
    return [profile.model_copy(deep=True) for profile in DEMO_USERS if profile.user_id != exclude_user_id]


def list_user_profiles(exclude_user_id: Optional[str] = None) -> list[UserProfile]:
    """列出所有用户画像；exclude_user_id 用于队友推荐时排除自己。"""
    with session_scope() as session:
        q = session.query(UserProfileModel)
        if exclude_user_id:
            q = q.filter(UserProfileModel.user_id != exclude_user_id)
        rows = q.all()
        return [_user_to_pydantic(m) for m in rows]


def save_user_profile(profile: UserProfile) -> UserProfile:
    with session_scope() as session:
        existing = session.get(UserProfileModel, profile.user_id)
        if existing is None:
            existing = UserProfileModel(user_id=profile.user_id)
            session.add(existing)
        existing.education_level = profile.education_level.value
        existing.grade = profile.grade.value
        existing.major = profile.major
        existing.skills = _dump_json(profile.skills)
        existing.experiences = _dump_json(profile.experiences)
        existing.weekly_available_hours = profile.weekly_available_hours
        existing.expected_team_size = profile.expected_team_size
        existing.privacy_consent = profile.privacy_consent
        if profile.display_name is not None:
            existing.display_name = profile.display_name
        if profile.persona is not None:
            existing.persona = profile.persona
        if profile.avatar is not None:
            existing.avatar = profile.avatar
    return profile


def delete_user_profile(user_id: str) -> dict:
    """删除画像及其项目数据；赛事官方数据不受影响。"""
    with session_scope() as session:
        profile = session.get(UserProfileModel, user_id)
        projects = session.query(UserProjectModel).filter(UserProjectModel.user_id == user_id).all()
        project_count = len(projects)
        item_count = sum(len(p.items) for p in projects)
        for project in projects:
            session.delete(project)
        if profile is not None:
            session.delete(profile)
        return {"deleted": profile is not None, "projects_deleted": project_count, "items_deleted": item_count}


def _project_to_pydantic(m: UserProjectModel) -> UserProject:
    comp = m.competition
    readiness = assess_recommendation_readiness(_competition_to_pydantic(comp), contest_now())
    profile = get_user_profile(m.user_id)
    eligible, gate_reasons = eligibility_gate(profile, _competition_to_pydantic(comp), contest_now()) if profile and readiness.ready else (False, [])
    readiness_reasons = list(readiness.reasons) + [r.reason for r in gate_reasons]
    if profile is None:
        readiness_reasons.append("个人画像不存在")
    ordered = sorted(m.items, key=lambda item: (item.sort_order, item.item_id))
    statuses = {item.item_id: item.status for item in ordered}
    payload_items: list[ProjectItem] = []
    for item in ordered:
        dependency_open = bool(
            item.depends_on_item_id
            and statuses.get(item.depends_on_item_id) not in {
                ProjectItemStatus.DONE.value,
                ProjectItemStatus.SKIPPED.value,
            }
        )
        is_blocked = bool(item.blocked_reason) or dependency_open or item.status == ProjectItemStatus.BLOCKED.value
        payload_items.append(
            ProjectItem(
                item_id=item.item_id,
                item_type=ProjectItemType(item.item_type),
                title=item.title,
                due_date=item.due_date,
                status=ProjectItemStatus(item.status),
                sort_order=item.sort_order,
                phase=item.phase or "execution",
                depends_on_item_id=item.depends_on_item_id,
                blocked_reason=(
                    item.blocked_reason
                    or ("前置任务尚未完成" if dependency_open else None)
                ),
                source_alert_id=item.source_alert_id,
                source_citation_id=item.source_citation_id,
                estimated_hours=item.estimated_hours,
                is_blocked=is_blocked,
            )
        )
    completed = sum(
        1 for item in payload_items if item.status in {ProjectItemStatus.DONE, ProjectItemStatus.SKIPPED}
    )
    progress = round(completed / len(payload_items) * 100) if payload_items else 0
    blocked_count = sum(1 for item in payload_items if item.is_blocked)
    overdue = sum(
        1 for item in payload_items
        if item.due_date and item.due_date < contest_now().date() and item.status not in {ProjectItemStatus.DONE, ProjectItemStatus.SKIPPED}
    )
    risk_level = "high" if overdue or blocked_count >= 2 or not readiness.ready or not eligible else (
        "medium" if blocked_count or (comp.registration_deadline and (comp.registration_deadline - contest_now().date()).days <= 14) else "low"
    )
    return UserProject(
        project_id=m.project_id,
        user_id=m.user_id,
        competition_id=m.competition_id,
        competition_name=comp.competition_name,
        document_year=comp.document_year,
        # 截止日期始终从赛事主表读取，项目表不保存副本。
        registration_deadline=comp.registration_deadline,
        submission_deadline=comp.submission_deadline,
        registration_deadline_at=comp.registration_deadline_at,
        submission_deadline_at=comp.submission_deadline_at,
        deadline_timezone_basis=comp.deadline_timezone_basis or "campus_default",
        status=ProjectStatus(m.status),
        created_at=m.created_at.isoformat(),
        recommendation_ready=readiness.ready and eligible,
        readiness_reasons=readiness_reasons,
        items=payload_items,
        progress_percent=progress,
        blocked_count=blocked_count,
        risk_level=risk_level,
    )


def list_user_projects(user_id: str) -> list[UserProject]:
    with session_scope() as session:
        rows = (
            session.query(UserProjectModel)
            .filter(UserProjectModel.user_id == user_id)
            .order_by(UserProjectModel.created_at.desc())
            .all()
        )
        for row in rows:
            _ensure_project_template(session, row, row.competition)
        session.flush()
        for row in rows:
            session.expire(row, ["items"])
        return [_project_to_pydantic(row) for row in rows]


def get_user_project(user_id: str, project_id: int) -> Optional[UserProject]:
    with session_scope() as session:
        row = session.get(UserProjectModel, project_id)
        if row is None or row.user_id != user_id:
            return None
        _ensure_project_template(session, row, row.competition)
        session.flush()
        session.expire(row, ["items"])
        return _project_to_pydantic(row)


def _get_or_create_project_model(session, user_id: str, comp: CompetitionModel) -> UserProjectModel:
    """在调用方事务中幂等创建项目及默认任务。"""
    existing = (
        session.query(UserProjectModel)
        .filter(
            UserProjectModel.user_id == user_id,
            UserProjectModel.competition_id == comp.competition_id,
        )
        .first()
    )
    if existing is not None:
        _ensure_project_template(session, existing, comp)
        return existing

    project = UserProjectModel(
        user_id=user_id,
        competition_id=comp.competition_id,
        status=ProjectStatus.PLANNED.value,
        created_at=contest_now().astimezone(timezone.utc).replace(tzinfo=None),
    )
    session.add(project)
    session.flush()

    _ensure_project_template(session, project, comp)
    return project


def _safe_due(base: Optional[date], days_before: int = 0, not_before: Optional[date] = None) -> Optional[date]:
    if base is None:
        return None
    due = base - timedelta(days=days_before)
    return min(base, max(due, not_before)) if not_before else due


def _defense_due(comp: CompetitionModel) -> Optional[date]:
    """答辩/证据包节点必须晚于提交节点，避免赛程字段倒置时出现反向流程。"""
    submission = comp.submission_deadline or comp.registration_deadline
    stated = [item for item in (comp.competition_start_date, comp.competition_end_date) if item]
    if submission is None:
        return min(stated) if stated else None
    later = [item for item in stated if item > submission]
    return min(later) if later else submission + timedelta(days=7)


def _ensure_project_template(session, project: UserProjectModel, comp: CompetitionModel) -> None:
    """幂等补齐七阶段参赛模板，并建立可解释的线性依赖。"""
    # 兼容旧版项目默认条目：先把已有条目归入阶段，再补缺，不制造重复任务。
    legacy_aliases = (
        ("qualification", ("核对", "资格", "官方证据")),
        ("team_topic", ("团队", "分工", "组队")),
        ("registration", ("报名",)),
    )
    used_legacy_phases: set[str] = set()
    for item in list(project.items):
        if item.item_type != ProjectItemType.TASK.value or (item.phase and item.phase != "execution"):
            continue
        for phase, keywords in legacy_aliases:
            if phase in used_legacy_phases:
                continue
            if any(keyword in item.title for keyword in keywords):
                item.phase = phase
                used_legacy_phases.add(phase)
                break
    created = project.created_at
    created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
    start = created.astimezone(contest_now().tzinfo).date()
    # Finish registration before the suggested design milestone, even when
    # official registration and submission close on the same day.
    registration_due = _safe_due(comp.registration_deadline, 7, start)
    solution_due = _safe_due(comp.submission_deadline, 42, start)
    if registration_due and solution_due:
        registration_due = min(registration_due, solution_due)
    production_start = max(start, registration_due or start)
    template = [
        ("qualification", "01 · 资格核对与官方证据确认", _safe_due(registration_due, 14, start), 2.0),
        ("team_topic", "02 · 组队分工与选题冻结", _safe_due(registration_due, 7, start), 6.0),
        ("registration", "03 · 完成报名与队伍信息确认", registration_due, 2.0),
        ("solution", "04 · 方案设计与评审指标映射", _safe_due(comp.submission_deadline, 42, production_start), 12.0),
        ("production", "05 · 作品制作与中期验收", _safe_due(comp.submission_deadline, 14, production_start), 36.0),
        ("submission", "06 · 提交前合规检查", comp.submission_deadline, 4.0),
        ("defense", "07 · 答辩演练与证据包准备", _defense_due(comp), 8.0),
    ]
    canonical_titles = {title for _phase, title, _due, _hours in template}
    all_items = session.query(ProjectItemModel).filter(ProjectItemModel.project_id == project.project_id).all()
    # 若旧版已有同一阶段任务，删除本次自动补齐的重复模板条目，保留用户原任务。
    replaced_ids: dict[int, int] = {}
    for phase, _title, _due, _hours in template:
        # 材料也可能标为 submission phase，但它们不是可替换的阶段任务。
        phase_items = [
            item for item in all_items
            if item.item_type == ProjectItemType.TASK.value and item.phase == phase
        ]
        legacy = [item for item in phase_items if item.title not in canonical_titles]
        if legacy:
            keep = legacy[0]
            for duplicate in phase_items:
                if duplicate is not keep and duplicate.title in canonical_titles:
                    replaced_ids[duplicate.item_id] = keep.item_id
                    session.delete(duplicate)
            keep.phase = phase
    session.flush()
    all_items = [
        item for item in session.query(ProjectItemModel).filter(ProjectItemModel.project_id == project.project_id).all()
        if item not in session.deleted
    ]
    if replaced_ids:
        for item in all_items:
            if item.depends_on_item_id in replaced_ids:
                item.depends_on_item_id = replaced_ids[item.depends_on_item_id]
        session.flush()
    # 阶段锚点只能是任务，材料同样带有 submission phase 时绝不能覆盖“提交前检查”任务。
    existing_by_phase = {
        item.phase: item for item in all_items
        if item.item_type == ProjectItemType.TASK.value and item.phase and item.phase != "execution"
    }
    valid_item_ids = {item.item_id for item in all_items}
    previous: Optional[ProjectItemModel] = None
    next_order = max((item.sort_order for item in all_items), default=-1) + 1
    for phase, title, due_date, hours in template:
        item = existing_by_phase.get(phase)
        if item is None:
            item = ProjectItemModel(
                project_id=project.project_id,
                item_type=ProjectItemType.TASK.value,
                title=title,
                due_date=due_date,
                status=ProjectItemStatus.TODO.value,
                sort_order=next_order,
                phase=phase,
                estimated_hours=hours,
            )
            session.add(item)
            session.flush()
            next_order += 1
        elif item.title == title and item.status not in {ProjectItemStatus.DONE.value, ProjectItemStatus.SKIPPED.value}:
            # 同步修复既有系统模板的日期；用户自行新增的任务不覆盖。
            item.due_date = due_date
        if previous is not None and (
            item.depends_on_item_id is None or item.depends_on_item_id not in valid_item_ids
        ):
            item.depends_on_item_id = previous.item_id
        previous = item
    # 新任务已 flush，重新读取后按阶段重建锚点，避免旧字典缺少新建的 submission
    # 而错误回退到 defense，造成“答辩 ↔ 材料”的反向阻塞。
    session.flush()
    all_items = session.query(ProjectItemModel).filter(ProjectItemModel.project_id == project.project_id).all()
    task_by_phase = {
        item.phase: item for item in all_items
        if item.item_type == ProjectItemType.TASK.value and item.phase
    }
    phase_order = ("qualification", "team_topic", "registration", "solution", "production", "submission", "defense")
    for index, phase in enumerate(phase_order):
        task = task_by_phase.get(phase)
        if task is None:
            continue
        expected = task_by_phase.get(phase_order[index - 1]) if index else None
        # 系统模板任务只能依赖前一阶段任务，不允许引用材料或后续阶段。
        task.depends_on_item_id = expected.item_id if expected else None

    material_due = comp.submission_deadline
    existing_materials = {item.title for item in all_items if item.item_type == ProjectItemType.MATERIAL.value}
    material_dependency = task_by_phase.get("production")
    for material in _load_json(comp.required_materials):
        if material in existing_materials:
            continue
        session.add(
            ProjectItemModel(
                project_id=project.project_id,
                item_type=ProjectItemType.MATERIAL.value,
                title=material,
                due_date=material_due,
                status=ProjectItemStatus.TODO.value,
                sort_order=next_order,
                phase="submission",
                depends_on_item_id=material_dependency.item_id if material_dependency else None,
            )
        )
        next_order += 1
    # 迁移已存在项目：材料统一在制作阶段后准备，不能把答辩作为前置。
    session.flush()
    for item in session.query(ProjectItemModel).filter(
        ProjectItemModel.project_id == project.project_id,
        ProjectItemModel.item_type == ProjectItemType.MATERIAL.value,
    ):
        item.depends_on_item_id = material_dependency.item_id if material_dependency else None
        if (
            comp.submission_deadline is None
            and item.phase == "submission"
            and item.title in _load_json(comp.required_materials)
            and item.due_date == comp.registration_deadline
        ):
            item.due_date = None
    _repair_project_dependency_cycles(session, project.project_id)


def _repair_project_dependency_cycles(session, project_id: int) -> None:
    """修复旧数据或人工编辑留下的循环依赖，确保项目始终存在可开始的节点。"""
    items = session.query(ProjectItemModel).filter(ProjectItemModel.project_id == project_id).all()
    by_id = {item.item_id: item for item in items}
    for start in items:
        path: list[int] = []
        seen_at: dict[int, int] = {}
        cursor = start
        while cursor and cursor.depends_on_item_id:
            if cursor.item_id in seen_at:
                cycle_ids = path[seen_at[cursor.item_id]:]
                cycle_items = [by_id[item_id] for item_id in cycle_ids if item_id in by_id]
                # 优先断开材料节点；否则断开排序最靠后的节点，保留前序工作链。
                victim = next((item for item in cycle_items if item.item_type == ProjectItemType.MATERIAL.value), None)
                victim = victim or max(cycle_items, key=lambda item: (item.sort_order, item.item_id))
                victim.depends_on_item_id = None
                break
            seen_at[cursor.item_id] = len(path)
            path.append(cursor.item_id)
            cursor = by_id.get(cursor.depends_on_item_id)


def _hydrate_projects(session, projects: list[UserProjectModel]) -> list[UserProject]:
    session.flush()
    result: list[UserProject] = []
    for project in projects:
        session.refresh(project)
        session.expire(project, ["items"])
        result.append(_project_to_pydantic(project))
    return result


def create_user_project(user_id: str, competition_id: str) -> UserProject:
    with session_scope() as session:
        profile = session.get(UserProfileModel, user_id)
        if profile is None:
            raise ValueError("profile_required")
        comp = session.get(CompetitionModel, competition_id)
        if comp is None:
            raise ValueError("competition_not_found")
        competition = _competition_to_pydantic(comp)
        # Keep existing downgraded projects, but do not create new ones from candidates.
        if not assess_source_readiness(competition).ready:
            raise ValueError("competition_basic_info_incomplete")
        if not is_registerable_now(competition, contest_now()):
            raise ValueError("competition_expired")
        if not eligibility_gate(_user_to_pydantic(profile), competition, contest_now())[0]:
            raise ValueError("profile_ineligible")
        return _hydrate_projects(
            session, [_get_or_create_project_model(session, user_id, comp)]
        )[0]


def create_user_projects_atomic(
    user_id: str,
    competition_ids: list[str],
    current: date | datetime | None = None,
) -> tuple[list[UserProject], list[dict]]:
    """在一个事务内重新校验并采用整个组合；任一项失败则不写入任何项目。"""
    current = current or contest_now()
    with session_scope() as session:
        profile = (
            session.query(UserProfileModel)
            .filter(UserProfileModel.user_id == user_id)
            .with_for_update()
            .first()
        )
        if profile is None:
            raise ValueError("profile_required")

        rows = (
            session.query(CompetitionModel)
            .filter(CompetitionModel.competition_id.in_(competition_ids))
            .with_for_update()
            .all()
        )
        competitions = {row.competition_id: row for row in rows}
        failures: list[dict] = []
        for competition_id in competition_ids:
            comp = competitions.get(competition_id)
            if comp is None:
                failures.append(
                    {"competition_id": competition_id, "reason": "competition_not_found"}
                )
                continue
            readiness = assess_recommendation_readiness(
                _competition_to_pydantic(comp), current
            )
            if not readiness.ready:
                failures.append(
                    {
                        "competition_id": competition_id,
                        "reason": "；".join(readiness.reasons),
                    }
                )
            else:
                eligible, reasons = eligibility_gate(_user_to_pydantic(profile), _competition_to_pydantic(comp), current)
                if not eligible:
                    failures.append({"competition_id": competition_id, "reason": "；".join(r.reason for r in reasons)})
        if failures:
            return [], failures

        projects = [
            _get_or_create_project_model(session, user_id, competitions[competition_id])
            for competition_id in competition_ids
        ]
        return _hydrate_projects(session, projects), []


def update_user_project(user_id: str, project_id: int, status: ProjectStatus) -> Optional[UserProject]:
    with session_scope() as session:
        row = session.get(UserProjectModel, project_id)
        if row is None or row.user_id != user_id:
            return None
        row.status = status.value
        session.flush()
        return _project_to_pydantic(row)


def delete_user_project(user_id: str, project_id: int) -> bool:
    with session_scope() as session:
        row = session.get(UserProjectModel, project_id)
        if row is None or row.user_id != user_id:
            return False
        session.delete(row)
        return True


def _project_item_to_pydantic(item: ProjectItemModel, session) -> ProjectItem:
    dependency_open = False
    if item.depends_on_item_id:
        dependency = session.get(ProjectItemModel, item.depends_on_item_id)
        dependency_open = bool(
            dependency is None
            or dependency.status not in {ProjectItemStatus.DONE.value, ProjectItemStatus.SKIPPED.value}
        )
    return ProjectItem(
        item_id=item.item_id,
        item_type=ProjectItemType(item.item_type),
        title=item.title,
        due_date=item.due_date,
        status=ProjectItemStatus(item.status),
        sort_order=item.sort_order,
        phase=item.phase or "execution",
        depends_on_item_id=item.depends_on_item_id,
        blocked_reason=item.blocked_reason or ("前置任务尚未完成" if dependency_open else None),
        source_alert_id=item.source_alert_id,
        source_citation_id=item.source_citation_id,
        estimated_hours=item.estimated_hours,
        is_blocked=bool(item.blocked_reason) or dependency_open or item.status == ProjectItemStatus.BLOCKED.value,
    )


def _validate_dependency(session, project_id: int, item_id: Optional[int], depends_on_item_id: Optional[int]) -> None:
    if depends_on_item_id is None:
        return
    dependency = session.get(ProjectItemModel, depends_on_item_id)
    if dependency is None or dependency.project_id != project_id:
        raise ValueError("dependency_not_in_project")
    if item_id is not None and item_id == depends_on_item_id:
        raise ValueError("dependency_cycle")
    cursor = dependency
    seen = {depends_on_item_id}
    for _ in range(100):
        parent_id = cursor.depends_on_item_id
        if parent_id is None:
            return
        if parent_id == item_id or parent_id in seen:
            raise ValueError("dependency_cycle")
        seen.add(parent_id)
        cursor = session.get(ProjectItemModel, parent_id)
        if cursor is None or cursor.project_id != project_id:
            raise ValueError("dependency_not_in_project")
    raise ValueError("dependency_cycle")


def add_project_item(
    user_id: str,
    project_id: int,
    item_type: ProjectItemType,
    title: str,
    due_date: Optional[date],
    *,
    phase: str = "execution",
    depends_on_item_id: Optional[int] = None,
    blocked_reason: Optional[str] = None,
    source_alert_id: Optional[int] = None,
    source_citation_id: Optional[int] = None,
    estimated_hours: Optional[float] = None,
) -> Optional[ProjectItem]:
    with session_scope() as session:
        project = session.get(UserProjectModel, project_id)
        if project is None or project.user_id != user_id:
            return None
        _validate_dependency(session, project_id, None, depends_on_item_id)
        if source_citation_id is not None:
            citation = session.get(CitationModel, source_citation_id)
            if citation is None or citation.competition_id != project.competition_id:
                raise ValueError("citation_not_in_project_competition")
        max_order = max((item.sort_order for item in project.items), default=-1)
        item = ProjectItemModel(
            project_id=project_id,
            item_type=item_type.value,
            title=title.strip(),
            due_date=due_date,
            status=ProjectItemStatus.TODO.value,
            sort_order=max_order + 1,
            phase=phase.strip() or "execution",
            depends_on_item_id=depends_on_item_id,
            blocked_reason=blocked_reason,
            source_alert_id=source_alert_id,
            source_citation_id=source_citation_id,
            estimated_hours=estimated_hours,
        )
        session.add(item)
        session.flush()
        return _project_item_to_pydantic(item, session)


def update_project_item(
    user_id: str,
    project_id: int,
    item_id: int,
    *,
    title: Optional[str],
    due_date: Optional[date],
    status: Optional[ProjectItemStatus],
    phase: Optional[str] = None,
    depends_on_item_id: Optional[int] = None,
    blocked_reason: Optional[str] = None,
    estimated_hours: Optional[float] = None,
) -> Optional[ProjectItem]:
    with session_scope() as session:
        project = session.get(UserProjectModel, project_id)
        item = session.get(ProjectItemModel, item_id)
        if project is None or project.user_id != user_id or item is None or item.project_id != project_id:
            return None
        if title is not None:
            item.title = title.strip()
        if due_date is not None:
            item.due_date = due_date
        if phase is not None:
            item.phase = phase.strip() or item.phase
        if depends_on_item_id is not None:
            _validate_dependency(session, project_id, item_id, depends_on_item_id)
            item.depends_on_item_id = depends_on_item_id
        if blocked_reason is not None:
            item.blocked_reason = blocked_reason.strip() or None
        if estimated_hours is not None:
            item.estimated_hours = estimated_hours
        if status is not None:
            if status in {ProjectItemStatus.IN_PROGRESS, ProjectItemStatus.DONE} and item.depends_on_item_id:
                dependency = session.get(ProjectItemModel, item.depends_on_item_id)
                if dependency and dependency.status not in {
                    ProjectItemStatus.DONE.value,
                    ProjectItemStatus.SKIPPED.value,
                }:
                    raise ValueError("dependency_open")
            item.status = status.value
        session.flush()
        return _project_item_to_pydantic(item, session)


def delete_project_item(user_id: str, project_id: int, item_id: int) -> bool:
    with session_scope() as session:
        project = session.get(UserProjectModel, project_id)
        item = session.get(ProjectItemModel, item_id)
        if project is None or project.user_id != user_id or item is None or item.project_id != project_id:
            return False
        session.delete(item)
        return True


def upsert_competition(comp: Competition) -> Competition:
    """创建或更新赛事（含 evidence 列表）。

    用于「上传→解析→来源确认」闭环：调用方关联官方原文 evidence，状态由
    统一完整性规则决定。幂等：按 competition_id 覆盖。
    """
    with session_scope() as session:
        cid = comp.competition_id
        existing = session.get(CompetitionModel, cid)
        if existing is None:
            existing = CompetitionModel(competition_id=cid)
            session.add(existing)

        existing.competition_name = comp.competition_name
        existing.document_year = comp.document_year
        existing.category = comp.category.value
        existing.organizer = comp.organizer
        existing.eligible_students = _dump_json([s.value for s in comp.eligible_students])
        existing.allowed_grades = (
            None if comp.allowed_grades is None else _dump_json([g.value for g in comp.allowed_grades])
        )
        existing.allowed_majors = (
            None if comp.allowed_majors is None else _dump_json(comp.allowed_majors)
        )
        existing.team_required = comp.team_required
        existing.team_min = comp.team_min
        existing.team_max = comp.team_max
        existing.registration_deadline = comp.registration_deadline
        existing.registration_deadline_at = comp.registration_deadline_at.isoformat() if comp.registration_deadline_at else None
        existing.submission_deadline = comp.submission_deadline
        existing.submission_deadline_at = comp.submission_deadline_at.isoformat() if comp.submission_deadline_at else None
        existing.deadline_timezone_basis = comp.deadline_timezone_basis
        existing.result_announcement_date = comp.result_announcement_date
        existing.competition_start_date = comp.competition_start_date
        existing.competition_end_date = comp.competition_end_date
        existing.award_settings = comp.award_settings
        existing.award_distribution = _dump_json(
            [a.model_dump(mode="json") for a in comp.award_distribution]
        )
        existing.brief_description = comp.brief_description
        existing.required_materials = _dump_json(comp.required_materials)
        existing.evaluation_dimensions = _dump_json(comp.evaluation_dimensions)
        existing.required_skills = _dump_json(comp.required_skills)
        existing.official_source_url = comp.official_source_url
        existing.source_acquired_date = comp.source_acquired_date
        existing.trusted_level = (
            comp.trusted_level.value if isinstance(comp.trusted_level, TrustedLevel) else comp.trusted_level
        )
        existing.data_status = (
            comp.data_status.value if isinstance(comp.data_status, DataStatus) else comp.data_status
        )
        existing.last_verified_at = comp.last_verified_at
        existing.official_source_status = comp.official_source_status
        existing.notes = comp.notes
        existing.doc_version = comp.doc_version

        # evidence：先删后插，保证与本次确认一致
        for old in list(existing.citations):
            session.delete(old)
        session.flush()
        for e in comp.evidence:
            session.add(
                CitationModel(
                    competition_id=cid,
                    field=e.field,
                    page=e.page,
                    source_text=e.source_text,
                    document_name=e.document_name,
                    source_url=e.source_url,
                    acquired_date=e.acquired_date,
                    last_verified_at=e.last_verified_at,
                    trusted_level=(
                        e.trusted_level.value
                        if isinstance(e.trusted_level, TrustedLevel)
                        else e.trusted_level
                    ),
                    document_id=e.document_id,
                    document_sha256=e.document_sha256,
                    page_rects=_dump_json([r.model_dump() for r in e.rects]),
                    anchor_quality=e.anchor_quality,
                    text_exact=e.text_exact or e.source_text,
                    text_prefix=e.text_prefix,
                    text_suffix=e.text_suffix,
                    text_start=e.text_start,
                    text_end=e.text_end,
                    anchor_confidence=e.anchor_confidence or (1.0 if e.anchor_quality == "exact" else 0.45),
                    anchor_status=e.anchor_status,
                )
            )
    return comp


# ---------------------------------------------------------------------------
# 不可变文档与证据预览
# ---------------------------------------------------------------------------


def store_document(filename: str, mime_type: str, content: bytes) -> dict:
    """按内容指纹保存官方文档；重复上传返回同一文档记录。"""
    digest = hashlib.sha256(content).hexdigest()
    with session_scope() as session:
        existing = session.query(DocumentModel).filter(DocumentModel.sha256 == digest).first()
        if existing is None:
            existing = DocumentModel(
                sha256=digest,
                filename=filename,
                mime_type=mime_type,
                byte_size=len(content),
                content=content,
            )
            session.add(existing)
            session.flush()
        return {
            "document_id": existing.document_id,
            "sha256": existing.sha256,
            "filename": existing.filename,
            "mime_type": existing.mime_type,
            "byte_size": existing.byte_size,
        }


def get_document(document_id: int, include_content: bool = False) -> Optional[dict]:
    with session_scope() as session:
        item = session.get(DocumentModel, document_id)
        if item is None:
            return None
        payload = {
            "document_id": item.document_id,
            "sha256": item.sha256,
            "filename": item.filename,
            "mime_type": item.mime_type,
            "byte_size": item.byte_size,
            "created_at": item.created_at.isoformat(),
        }
        if include_content:
            payload["content"] = bytes(item.content)
        return payload


def get_citation(citation_id: int) -> Optional[Citation]:
    with session_scope() as session:
        item = session.get(CitationModel, citation_id)
        return _citation_to_pydantic(item) if item is not None else None


def get_citation_competition_id(citation_id: int) -> Optional[str]:
    with session_scope() as session:
        item = session.get(CitationModel, citation_id)
        return item.competition_id if item is not None else None


def update_citation_locator(
    citation_id: int,
    document_id: int,
    document_sha256: str,
    rects: list[dict],
    anchor_quality: str = "exact",
    *,
    page: Optional[int] = None,
    text_exact: Optional[str] = None,
    text_prefix: Optional[str] = None,
    text_suffix: Optional[str] = None,
    text_start: Optional[int] = None,
    text_end: Optional[int] = None,
    anchor_confidence: Optional[float] = None,
    anchor_status: Optional[str] = None,
) -> bool:
    with session_scope() as session:
        item = session.get(CitationModel, citation_id)
        if item is None:
            return False
        item.document_id = document_id
        item.document_sha256 = document_sha256
        item.page_rects = _dump_json(rects)
        item.anchor_quality = anchor_quality
        if page is not None:
            item.page = page
        item.text_exact = text_exact or item.text_exact or item.source_text
        if text_prefix is not None:
            item.text_prefix = text_prefix
        if text_suffix is not None:
            item.text_suffix = text_suffix
        if text_start is not None:
            item.text_start = text_start
        if text_end is not None:
            item.text_end = text_end
        if anchor_confidence is not None:
            item.anchor_confidence = max(0.0, min(1.0, float(anchor_confidence)))
        item.anchor_status = anchor_status or item.anchor_status or "original"
        return True


def mark_citation_anchor_status(citation_id: int, status: str, confidence: float = 0.0) -> bool:
    """只更新定位状态，不改变事实、原文或其来源版本。"""
    if status not in {"original", "relocated", "needs_review", "invalid"}:
        raise ValueError("invalid_anchor_status")
    with session_scope() as session:
        item = session.get(CitationModel, citation_id)
        if item is None:
            return False
        item.anchor_status = status
        item.anchor_confidence = max(0.0, min(1.0, confidence))
        return True


# ---------------------------------------------------------------------------
# 动态赛事雷达持久化
# ---------------------------------------------------------------------------


def _watch_dict(item: SourceWatchModel, competition_name: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    age_hours = None
    if item.last_success_at:
        age_hours = round(max(0.0, (now - item.last_success_at).total_seconds() / 3600), 1)
    if not item.enabled:
        health_score, health_status = 0, "paused"
    elif item.last_status == "new" or (not item.last_success_at and item.consecutive_failures == 0):
        health_score, health_status = 0, "unknown"
    elif item.last_status == "ok" and item.consecutive_failures == 0:
        health_score = 65 if age_hours is None or age_hours > item.interval_hours * 2 else 100
        health_status = "healthy" if health_score >= 80 else "degraded"
    else:
        health_score = max(0, min(79, 80 - item.consecutive_failures * 22))
        health_status = "degraded" if health_score >= 45 else "critical"
    def utc_iso(value):
        return value.replace(tzinfo=timezone.utc).isoformat() if value else None
    return {
        "watch_id": item.watch_id,
        "competition_id": item.competition_id,
        "competition_name": competition_name,
        "source_url": item.source_url,
        "source_type": item.source_type,
        "css_selector": item.css_selector,
        "include_selector": item.include_selector or item.css_selector,
        "exclude_selector": item.exclude_selector,
        "ignore_regex": item.ignore_regex,
        "trigger_terms": _load_json(item.trigger_terms),
        "fetch_mode": item.fetch_mode,
        "timezone": item.timezone_name,
        "interval_hours": item.interval_hours,
        "enabled": item.enabled,
        "last_checked_at": utc_iso(item.last_checked_at),
        "etag": item.etag,
        "last_modified": item.last_modified,
        "last_content_hash": item.last_content_hash,
        "last_status": item.last_status,
        "consecutive_failures": item.consecutive_failures,
        "last_error": item.last_error,
        "last_success_at": utc_iso(item.last_success_at),
        "next_scan_at": utc_iso(item.next_scan_at),
        "last_latency_ms": item.last_latency_ms,
        "last_content_bytes": item.last_content_bytes,
        "age_hours": age_hours,
        "health_score": health_score,
        "health_status": health_status,
        "connector_pipeline": ["discover", "fetch", "normalize", "snapshot", "diff", "review"],
    }


def create_source_watch(
    competition_id: str,
    source_url: str,
    source_type: str = "auto",
    css_selector: Optional[str] = None,
    include_selector: Optional[str] = None,
    exclude_selector: Optional[str] = None,
    ignore_regex: Optional[str] = None,
    trigger_terms: Optional[list[str]] = None,
    fetch_mode: str = "http",
    timezone: str = "Asia/Shanghai",
    interval_hours: int = 24,
) -> dict:
    with session_scope() as session:
        comp = session.get(CompetitionModel, competition_id)
        if comp is None:
            raise ValueError("competition_not_found")
        existing = (
            session.query(SourceWatchModel)
            .filter(
                SourceWatchModel.competition_id == competition_id,
                SourceWatchModel.source_url == source_url,
            )
            .first()
        )
        if existing is None:
            existing = SourceWatchModel(
                competition_id=competition_id,
                source_url=source_url,
                source_type=source_type,
                css_selector=css_selector or include_selector,
                include_selector=include_selector or css_selector,
                exclude_selector=exclude_selector,
                ignore_regex=ignore_regex,
                trigger_terms=_dump_json(trigger_terms or []),
                fetch_mode=fetch_mode,
                timezone_name=timezone,
                interval_hours=interval_hours,
            )
            session.add(existing)
            session.flush()
        else:
            existing.source_type = source_type
            existing.css_selector = css_selector or include_selector or existing.css_selector
            existing.include_selector = include_selector or css_selector or existing.include_selector
            existing.exclude_selector = exclude_selector
            existing.ignore_regex = ignore_regex
            existing.trigger_terms = _dump_json(trigger_terms or [])
            existing.fetch_mode = fetch_mode
            existing.timezone_name = timezone
            existing.interval_hours = interval_hours
        return _watch_dict(existing, comp.competition_name)


def list_source_watches(enabled_only: bool = False) -> list[dict]:
    with session_scope() as session:
        query = session.query(SourceWatchModel, CompetitionModel.competition_name).join(
            CompetitionModel, CompetitionModel.competition_id == SourceWatchModel.competition_id
        )
        if enabled_only:
            query = query.filter(SourceWatchModel.enabled.is_(True))
        return [_watch_dict(w, name) for w, name in query.order_by(SourceWatchModel.watch_id).all()]


def get_source_watch(watch_id: int) -> Optional[dict]:
    with session_scope() as session:
        row = (
            session.query(SourceWatchModel, CompetitionModel.competition_name)
            .join(CompetitionModel, CompetitionModel.competition_id == SourceWatchModel.competition_id)
            .filter(SourceWatchModel.watch_id == watch_id)
            .first()
        )
        return _watch_dict(row[0], row[1]) if row else None


def latest_source_snapshot(watch_id: int) -> Optional[dict]:
    with session_scope() as session:
        item = (
            session.query(SourceSnapshotModel)
            .filter(SourceSnapshotModel.watch_id == watch_id)
            .order_by(SourceSnapshotModel.snapshot_id.desc())
            .first()
        )
        if item is None:
            return None
        return {
            "snapshot_id": item.snapshot_id,
            "watch_id": item.watch_id,
            "fetched_at": item.fetched_at.isoformat(),
            "http_status": item.http_status,
            "content_hash": item.content_hash,
            "normalized_text": item.normalized_text,
            "document_id": item.document_id,
            "response_etag": item.response_etag,
            "response_last_modified": item.response_last_modified,
        }


def save_source_snapshot(
    watch_id: int,
    http_status: int,
    content_hash: str,
    normalized_text: str,
    document_id: Optional[int] = None,
    response_etag: Optional[str] = None,
    response_last_modified: Optional[str] = None,
    latency_ms: Optional[int] = None,
    content_bytes: Optional[int] = None,
) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as session:
        item = SourceSnapshotModel(
            watch_id=watch_id,
            fetched_at=now,
            http_status=http_status,
            content_hash=content_hash,
            normalized_text=normalized_text,
            document_id=document_id,
            response_etag=response_etag,
            response_last_modified=response_last_modified,
        )
        session.add(item)
        watch = session.get(SourceWatchModel, watch_id)
        if watch is None:
            raise ValueError("watch_not_found")
        watch.last_checked_at = now
        watch.etag = response_etag
        watch.last_modified = response_last_modified
        watch.last_content_hash = content_hash
        watch.last_status = "ok"
        watch.consecutive_failures = 0
        watch.last_error = None
        watch.last_success_at = now
        watch.next_scan_at = now + timedelta(hours=watch.interval_hours)
        watch.last_latency_ms = latency_ms
        watch.last_content_bytes = content_bytes
        session.flush()
        return {"snapshot_id": item.snapshot_id, "content_hash": item.content_hash}


def record_watch_failure(watch_id: int, error: str) -> None:
    with session_scope() as session:
        watch = session.get(SourceWatchModel, watch_id)
        if watch is None:
            return
        watch.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        watch.next_scan_at = watch.last_checked_at + timedelta(hours=watch.interval_hours)
        watch.consecutive_failures += 1
        watch.last_error = error[:1000]
        watch.last_status = "stale" if watch.consecutive_failures >= 3 else "error"


def create_change_event(
    watch_id: int,
    old_snapshot_id: Optional[int],
    new_snapshot_id: int,
    severity: str,
    change_type: str,
    summary: str,
    diff_text: str,
    affected_fields: list[str],
    field_changes: Optional[list[dict]] = None,
    proposed_changes: Optional[dict] = None,
) -> dict:
    with session_scope() as session:
        item = SourceChangeEventModel(
            watch_id=watch_id,
            old_snapshot_id=old_snapshot_id,
            new_snapshot_id=new_snapshot_id,
            severity=severity,
            change_type=change_type,
            summary=summary,
            diff_text=diff_text[:30000],
            affected_fields=_dump_json(affected_fields),
            field_changes=_dump_json(field_changes or []),
            proposed_changes=_dump_json(proposed_changes or {}),
        )
        session.add(item)
        session.flush()
        return {"event_id": item.event_id, "status": item.status}


def _event_dict(
    event: SourceChangeEventModel,
    watch: SourceWatchModel,
    name: str,
    session=None,
) -> dict:
    project_count = 0
    task_count = 0
    if session is not None:
        project_ids = [
            row[0]
            for row in session.query(UserProjectModel.project_id)
            .filter(UserProjectModel.competition_id == watch.competition_id)
            .all()
        ]
        project_count = len(project_ids)
        if project_ids:
            task_count = session.query(ProjectItemModel).filter(
                ProjectItemModel.project_id.in_(project_ids),
                ProjectItemModel.status.notin_([ProjectItemStatus.DONE.value, ProjectItemStatus.SKIPPED.value]),
            ).count()
    return {
        "event_id": event.event_id,
        "watch_id": event.watch_id,
        "competition_id": watch.competition_id,
        "competition_name": name,
        "source_url": watch.source_url,
        "detected_at": event.detected_at.isoformat(),
        "severity": event.severity,
        "change_type": event.change_type,
        "summary": event.summary,
        "diff_text": event.diff_text,
        "affected_fields": _load_json(event.affected_fields),
        "field_changes": _load_json(event.field_changes),
        "proposed_changes": json.loads(event.proposed_changes or "{}"),
        "affected_project_count": project_count,
        "affected_task_count": task_count,
        "impact_summary": (
            f"影响 {project_count} 个参赛项目、{task_count} 个未完成任务"
            if project_count else "当前没有已采用项目受影响"
        ),
        "status": event.status,
        "review_note": event.review_note,
        "reviewed_at": event.reviewed_at.isoformat() if event.reviewed_at else None,
    }


def list_change_events(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    with session_scope() as session:
        query = (
            session.query(SourceChangeEventModel, SourceWatchModel, CompetitionModel.competition_name)
            .join(SourceWatchModel, SourceWatchModel.watch_id == SourceChangeEventModel.watch_id)
            .join(CompetitionModel, CompetitionModel.competition_id == SourceWatchModel.competition_id)
        )
        if status:
            query = query.filter(SourceChangeEventModel.status == status)
        rows = query.order_by(SourceChangeEventModel.event_id.desc()).limit(limit).all()
        return [_event_dict(event, watch, name, session) for event, watch, name in rows]


def review_change_event(event_id: int, action: str, note: Optional[str] = None) -> Optional[dict]:
    """审核变化；仅把确定性解析出的字段写回，并为受影响项目创建提醒。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as session:
        event = session.get(SourceChangeEventModel, event_id)
        if event is None:
            return None
        if event.status != "pending":
            watch = session.get(SourceWatchModel, event.watch_id)
            comp = session.get(CompetitionModel, watch.competition_id)
            return _event_dict(event, watch, comp.competition_name, session)
        event.status = "approved" if action == "approve" else "rejected"
        event.review_note = note
        event.reviewed_at = now
        watch = session.get(SourceWatchModel, event.watch_id)
        comp = session.get(CompetitionModel, watch.competition_id)
        if action == "approve":
            proposed = json.loads(event.proposed_changes or "{}")
            checked_at = contest_now().date().isoformat()
            for field, change in proposed.items():
                if field not in {"registration_deadline", "submission_deadline"}:
                    continue
                try:
                    value = date.fromisoformat(str(change.get("value")))
                except (ValueError, TypeError):
                    continue
                setattr(comp, field, value)
                # A date-only change cannot carry forward the old document's clock or anchors.
                setattr(comp, field + "_at", None)
                comp.data_status = DataStatus.UNVERIFIED.value
                comp.trusted_level = TrustedLevel.B.value
                for old in comp.citations:
                    if old.field in {field, field + "_at"}:
                        old.anchor_status = "needs_review"
                citation = next((c for c in comp.citations if c.field == field), None)
                if citation is None:
                    citation = CitationModel(competition_id=comp.competition_id, field=field)
                    session.add(citation)
                citation.page = None
                citation.source_text = change.get("source_text") or event.summary
                citation.document_name = "官网变更快照"
                citation.source_url = watch.source_url
                citation.acquired_date = checked_at
                citation.last_verified_at = checked_at
                citation.trusted_level = comp.trusted_level
                citation.anchor_quality = "approximate"
                citation.anchor_status = "needs_review"
                citation.document_id = None
                citation.document_sha256 = None
                citation.page_rects = "[]"
                citation.text_exact = citation.source_text
                citation.text_prefix = citation.text_suffix = None
                citation.text_start = citation.text_end = None
                citation.anchor_confidence = 0.0
            comp.last_verified_at = checked_at
            comp.doc_version = f"{comp.document_year}_radar_{event.event_id}"
            project_users = (
                session.query(UserProjectModel.project_id, UserProjectModel.user_id)
                .filter(UserProjectModel.competition_id == comp.competition_id)
                .all()
            )
            affected_fields = set(_load_json(event.affected_fields))
            for project_id, user_id in project_users:
                alert = UserAlertModel(
                    user_id=user_id,
                    event_id=event.event_id,
                    competition_id=comp.competition_id,
                    title=f"{comp.competition_name} 官方信息发生变化",
                    message=event.summary,
                    project_id=project_id,
                )
                session.add(alert)
                session.flush()
                phases = {"qualification", "team_topic"} if "registration_deadline" in affected_fields else set()
                if "submission_deadline" in affected_fields:
                    phases.update({"solution", "production", "submission"})
                if phases:
                    for task in session.query(ProjectItemModel).filter(
                        ProjectItemModel.project_id == project_id,
                        ProjectItemModel.phase.in_(phases),
                        ProjectItemModel.status.notin_([ProjectItemStatus.DONE.value, ProjectItemStatus.SKIPPED.value]),
                    ):
                        task.source_alert_id = alert.alert_id
                        task.blocked_reason = "官方关键信息已更新，请先处理雷达收件箱"
        session.flush()
        return _event_dict(event, watch, comp.competition_name, session)


def list_user_alerts(user_id: str, limit: int = 50) -> list[dict]:
    with session_scope() as session:
        items = (
            session.query(UserAlertModel)
            .filter(UserAlertModel.user_id == user_id)
            .order_by(UserAlertModel.alert_id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "alert_id": item.alert_id,
                "event_id": item.event_id,
                "competition_id": item.competition_id,
                "title": item.title,
                "message": item.message,
                "is_read": item.is_read,
                "status": item.status or "pending",
                "action_note": item.action_note,
                "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
                "project_id": item.project_id,
                "item_id": item.item_id,
                "actions_available": (
                    ["accept", "ignore", "create_task", "replan"]
                    if (item.status or "pending") == "pending" else []
                ),
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ]


def act_on_user_alert(
    user_id: str,
    alert_id: int,
    action: str,
    note: Optional[str] = None,
) -> Optional[dict]:
    """处理雷达收件箱；转任务时保留事件与项目来源链。"""
    if action not in {"accept", "ignore", "create_task", "replan"}:
        raise ValueError("invalid_alert_action")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as session:
        alert = session.get(UserAlertModel, alert_id)
        if alert is None or alert.user_id != user_id:
            return None
        task_payload = None
        if action == "create_task":
            project = (
                session.query(UserProjectModel)
                .filter(
                    UserProjectModel.user_id == user_id,
                    UserProjectModel.competition_id == alert.competition_id,
                )
                .first()
            )
            if project is None:
                raise ValueError("project_not_found")
            item = session.get(ProjectItemModel, alert.item_id) if alert.item_id else None
            if item is None:
                comp = project.competition
                due = comp.registration_deadline or comp.submission_deadline
                item = ProjectItemModel(
                    project_id=project.project_id,
                    item_type=ProjectItemType.TASK.value,
                    title=f"雷达响应 · {alert.title}",
                    due_date=due,
                    status=ProjectItemStatus.TODO.value,
                    sort_order=max((i.sort_order for i in project.items), default=-1) + 1,
                    phase="risk_response",
                    blocked_reason=None,
                    source_alert_id=alert.alert_id,
                    estimated_hours=1.0,
                )
                session.add(item)
                session.flush()
            alert.project_id = project.project_id
            alert.item_id = item.item_id
            alert.status = "task_created"
            task_payload = _project_item_to_pydantic(item, session).model_dump(mode="json")
        elif action == "replan":
            alert.status = "replan_requested"
        else:
            alert.status = "accepted" if action == "accept" else "ignored"
        alert.is_read = True
        alert.action_note = note
        alert.resolved_at = now
        session.query(ProjectItemModel).filter(
            ProjectItemModel.source_alert_id == alert.alert_id,
            ProjectItemModel.blocked_reason == "官方关键信息已更新，请先处理雷达收件箱",
        ).update({ProjectItemModel.blocked_reason: None}, synchronize_session=False)
        session.flush()
        payload = {
            "alert_id": alert.alert_id,
            "event_id": alert.event_id,
            "competition_id": alert.competition_id,
            "status": alert.status,
            "project_id": alert.project_id,
            "item_id": alert.item_id,
            "replan_required": action == "replan",
        }
        if task_payload:
            payload["task"] = task_payload
        return payload


def create_task_from_agent(
    user_id: str,
    competition_id: str,
    title: str,
    due_date: Optional[date] = None,
    source_citation_id: Optional[int] = None,
    estimated_hours: Optional[float] = None,
) -> ProjectItem:
    """把 Agent 建议转为可执行任务，并保留所用引用。"""
    with session_scope() as session:
        profile = session.get(UserProfileModel, user_id)
        if profile is None:
            raise ValueError("profile_required")
        comp = session.get(CompetitionModel, competition_id)
        if comp is None:
            raise ValueError("competition_not_found")
        competition = _competition_to_pydantic(comp)
        if not assess_recommendation_readiness(competition, contest_now()).ready:
            raise ValueError("competition_not_ready")
        if not eligibility_gate(_user_to_pydantic(profile), competition, contest_now())[0]:
            raise ValueError("profile_ineligible")
        if source_citation_id is not None:
            citation = session.get(CitationModel, source_citation_id)
            if citation is None or citation.competition_id != competition_id:
                raise ValueError("citation_not_in_project_competition")
        project = _get_or_create_project_model(session, user_id, comp)
        session.flush()
        item = ProjectItemModel(
            project_id=project.project_id,
            item_type=ProjectItemType.TASK.value,
            title=title.strip(),
            due_date=due_date,
            status=ProjectItemStatus.TODO.value,
            sort_order=max((i.sort_order for i in project.items), default=-1) + 1,
            phase="agent_action",
            source_citation_id=source_citation_id,
            estimated_hours=estimated_hours,
        )
        session.add(item)
        session.flush()
        return _project_item_to_pydantic(item, session)


if __name__ == "__main__":
    init_db()
    n = seed_all()
    print(f"[db] 初始化完成，赛事已 seed {n} 条。")
    comps = get_all_competitions()
    print(f"[db] 当前赛事总数：{len(comps)}")
    for c in comps[:5]:
        print(f"  - {c.competition_id} ({c.category.value}, {c.document_year}) evidence={len(c.evidence)}")
