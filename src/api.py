"""FastAPI 接口层 —— 接入真实数据库（SQLite / PostgreSQL 可切换）。

运行：
    uvicorn api:app --reload --port 8000

所有响应结构已在 schemas.py 冻结（competition/timeline/requirements/sources/verification）。
数据库由 db.py 管理：首次启动自动建表并 seed 21 份赛事数据；
用户画像写入真正持久化（不再原样回显）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4


def _load_dotenv() -> None:
    """零依赖加载项目根目录 .env（本地开发用）。

    仅在文件存在时读取，且**不覆盖已存在的环境变量**——因此 Render 等平台
    直接注入的变量优先，线上无 .env 时此函数静默跳过，绝不影响生产。
    """
    for base in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        env_path = base / ".env"
        if not env_path.is_file():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:  # noqa: BLE001 - .env 解析失败不应阻断启动
            pass
        break


_load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
from rag.store import get_rag, embedding_backend
from recommendation.engine import eligibility_gate, recommend_for_user, recommend_teammates
from trust import assess_recommendation_readiness, assess_source_readiness
from contest_clock import contest_now
from schemas import (
    Citation,
    AgentTaskCreate,
    Competition,
    CompetitionCategory,
    DataStatus,
    PortfolioApplyRequest,
    PortfolioOptimizeResponse,
    PortfolioPreferences,
    RadarEventReview,
    RadarAlertAction,
    RadarWatchCreate,
    RecommendationResult,
    ProjectCreate,
    ProjectItem,
    ProjectItemCreate,
    ProjectItemUpdate,
    ProjectUpdate,
    UserProject,
    TrustedLevel,
    UserProfile,
    grades_for_education,
    EducationLevel,
)

import parsing.pdf_extractor as pdf_extractor
from admin import suggest as extract_suggest
from agent.graph import run_agent  # LangGraph 闭环：意图路由→隔离检索→门控→评分→组队文案
from agent.llm import is_llm_enabled  # 可选 LLM 润色开关（无 key 自动关闭）
from portfolio.optimizer import optimize_portfolios
from radar.service import run_all as run_radar, validate_public_url, validate_watch_configuration
from deployment_security import validate_production_configuration, demo_login_enabled, is_production

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "").strip()
_ADMIN_API_KEY = APIKeyHeader(name="X-Admin-Token", auto_error=False)


def _require_admin_token(token: str | None = Depends(_ADMIN_API_KEY)) -> None:
    if not _ADMIN_API_TOKEN or _ADMIN_API_TOKEN.startswith("replace-with-"):
        raise HTTPException(status_code=503, detail="数据维护接口未启用")
    if not token or not secrets.compare_digest(token, _ADMIN_API_TOKEN):
        raise HTTPException(status_code=401, detail="管理员令牌无效")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_configuration()
    # 启动时建表 + seed（幂等）
    db.init_db()
    # 从 DB 灌入赛事隔离 RAG（幂等：按 competition_id 覆盖），保证启动即可检索
    try:
        stats = get_rag().seed_from_db()
        print(f"[rag] 启动灌库完成：{len(stats)} 个赛事、{sum(stats.values())} 个片段，backend={embedding_backend()}")
    except Exception as exc:  # RAG 失败不应阻断主服务
        print(f"[rag] 启动灌库失败（不影响主服务）：{exc}")
    yield


app = FastAPI(title="校园科创导航智能体", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[v.strip() for v in os.getenv("CORS_ALLOWED_ORIGINS", "" if is_production() else "*").split(",") if v.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 用户级签名会话：绑定 /api/users/{user_id} 资源所有权
# ---------------------------------------------------------------------------
_AUTH_TOKEN_TTL_SECONDS = max(900, min(86400, int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "28800"))))
_AUTH_TOKEN_SECRET = (os.getenv("AUTH_TOKEN_SECRET") or secrets.token_urlsafe(48)).encode("utf-8")
_USER_API_RE = re.compile(r"^/api/users(?:/([^/]+))?(?:/|$)")


def _token_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token_b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _issue_access_token(username: str) -> str:
    now = int(time.time())
    payload = json.dumps(
        {"sub": username, "iat": now, "exp": now + _AUTH_TOKEN_TTL_SECONDS, "v": 1},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(_AUTH_TOKEN_SECRET, payload, hashlib.sha256).digest()
    return f"{_token_b64encode(payload)}.{_token_b64encode(signature)}"


def _access_token_subject(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _token_b64decode(encoded_payload)
        supplied_signature = _token_b64decode(encoded_signature)
        expected_signature = hmac.new(_AUTH_TOKEN_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        data = json.loads(payload.decode("utf-8"))
        subject = str(data.get("sub") or "")
        if int(data.get("exp") or 0) <= int(time.time()):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", subject):
            return None
        return subject
    except Exception:
        return None


@app.middleware("http")
async def enforce_user_resource_scope(request: Request, call_next):
    """保护用户画像、项目、组合、运行历史和情报收件箱。

    公开赛事、官方证据、健康检查与不带 user_id 的通用问答仍可访问；
    带用户画像的 Agent 问答必须与会话主体一致。
    """
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    user_match = _USER_API_RE.match(path)
    requested_user = unquote(user_match.group(1)) if user_match and user_match.group(1) else None
    agent_user = request.query_params.get("user_id") if path == "/api/agent/ask" else None
    replay_run_id = None
    replay_match = re.fullmatch(r"/api/agent/runs/([^/]+)/replay", path)
    if replay_match:
        replay_run_id = unquote(replay_match.group(1))

    requires_session = bool(user_match or agent_user or replay_run_id)
    if not requires_session:
        return await call_next(request)

    subject = _access_token_subject(request.headers.get("Authorization"))
    if subject is None:
        return JSONResponse(status_code=401, content={"detail": "请先登录或会话已过期"})
    target_user = requested_user or agent_user
    if target_user and not secrets.compare_digest(subject, target_user):
        return JSONResponse(status_code=403, content={"detail": "无权访问其他用户的数据"})
    if replay_run_id:
        saved = db.get_agent_run(replay_run_id)
        if saved and saved.get("user_id") and not secrets.compare_digest(subject, saved["user_id"]):
            return JSONResponse(status_code=403, content={"detail": "无权回放其他用户的运行"})
    request.state.user_id = subject
    return await call_next(request)


@app.get("/health", tags=["系统"])
def health() -> dict:
    """后端健康检查。"""
    try:
        with db.get_engine().connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        database_status = "connected"
    except Exception:
        database_status = "unavailable"
    return {
        "status": "ok" if database_status == "connected" else "degraded",
        "mock": False,
        "database": database_status,
        "date": contest_now().date().isoformat(),
        "embedding_backend": embedding_backend(),
        "llm_backend": "openai-compatible" if is_llm_enabled() else "disabled",
    }


@app.get("/api/system/quality", tags=["系统"])
def quality_dashboard() -> dict:
    """公开展示冻结评测结果与当前运行数据，便于评委核验工程质量。"""
    def read_json(name: str) -> dict:
        path = PROJECT_ROOT / "evals" / name
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    regression = read_json("metrics.json")
    formal = read_json("formal_results.json")
    regression_suite = formal.get("regression") or {}
    watches = db.list_source_watches()
    events = db.list_change_events(limit=200)
    dataset_files = sorted((PROJECT_ROOT / "data/ground_truth/samples").glob("*.json"))
    dataset_sha256 = hashlib.sha256(b"".join(path.read_bytes() for path in dataset_files)).hexdigest()
    competitions = db.list_competitions()
    from evaluation_provenance import evaluation_is_current

    golden_current = evaluation_is_current(regression, PROJECT_ROOT, competitions)
    return {
        "evaluated_at": formal.get("evaluated_at"),
        "golden_set": {
            "evaluation_type": "generated_template_regression",
            "cases": regression.get("total_cases", 0),
            "passed": regression.get("passed", 0),
            "current": golden_current,
            "evaluated_at": regression.get("evaluated_at"),
            "intent_accuracy": regression.get("intent_acc") if golden_current else None,
            "citation_recall": regression.get("citation_recall") if golden_current else None,
            "gate_consistency": regression.get("gate_consistency") if golden_current else None,
        },
        "formal_checks": formal.get("formal_summary", {}),
        "formal_metrics": formal.get("metrics", {}),
        "regression": {
            "tests_run": regression_suite.get("tests_run", 0),
            "passed": regression_suite.get("passed", 0),
            "failures": regression_suite.get("failures", 0),
            "errors": regression_suite.get("errors", 0),
            "successful": bool(regression_suite.get("successful")),
        },
        "environment": formal.get("environment", {}),
        "dataset": {
            "total": len(competitions),
            "source_complete": sum(assess_recommendation_readiness(comp).ready for comp in competitions),
            "registerable_ready": sum(assess_recommendation_readiness(comp, contest_now()).ready for comp in competitions),
            "evaluation_dataset_matches": evaluation_is_current(formal, PROJECT_ROOT, competitions),
        },
        "runtime": {
            "source_count": len(watches),
            "healthy_sources": sum(1 for item in watches if item.get("health_status") == "healthy"),
            "pending_human_reviews": sum(1 for event in events if event.get("status") == "pending"),
            "manual_review_gate": True,
            "ssrf_protection": True,
            "signed_user_sessions": True,
        },
        "note": "冻结报告与运行态分开展示；模板问答并非人工金标，门控一致率不等同于独立资格准确率。模板结果仅在数据、源码与数据库快照一致时展示。",
    }


@app.get("/api/competitions", tags=["赛事"])
def list_competitions(
    category: str | None = Query(None, description="programming/modeling/innovation/software"),
    year: int | None = Query(None, description="文档年份，避免跨年混用"),
    readiness: str = Query("all", pattern="^(all|ready|candidate)$"),
    limit: int | None = Query(None, ge=1, le=200, description="分页大小（默认全量，供前端兼容）"),
    offset: int = Query(0, ge=0, description="分页偏移"),
) -> list[dict]:
    """赛事目录；可按类别、年份与是否具备正式推荐资格过滤。

    分页为可选能力：不传 limit 时返回全量（前端本地过滤依赖此行为，保持兼容）；
    limit 限制返回记录数；可信判定仍覆盖筛选后的记录，不承诺延迟与分页大小线性变化。
    """
    # 兼容直接调用（测试/脚本未走 FastAPI 解析时，Query 默认值仍是 Query 对象）。
    category = category if isinstance(category, str) else None
    year = int(year) if isinstance(year, int) else None
    readiness = readiness if isinstance(readiness, str) else "all"
    offset = int(offset) if isinstance(offset, int) else 0
    if not isinstance(limit, int):
        limit = None
    rows: list[dict] = []
    for comp in db.list_competitions(category=category, year=year):
        assessment = assess_recommendation_readiness(comp, contest_now())
        if readiness == "ready" and not assessment.ready:
            continue
        if readiness == "candidate" and assessment.ready:
            continue
        payload = comp.model_dump(mode="json")
        payload["recommendation_ready"] = assessment.ready
        payload["readiness_reasons"] = list(assessment.reasons)
        rows.append(payload)
    if limit is not None:
        rows = rows[offset : offset + limit]
    return rows


@app.get("/api/competitions/{competition_id}", tags=["赛事"])
def competition_detail(competition_id: str) -> dict:
    """赛事详情 —— 统一返回 competition/timeline/requirements/sources/verification。"""
    detail = db.get_competition_detail(competition_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return detail.model_dump(mode="json")


@app.get("/api/users", tags=["用户"])
def list_users() -> list[dict]:
    """可登录用户列表（供登录页展示不同「特色」身份，一键切换体验千人千面）。

    只返回展示所需的轻量字段，不含敏感信息。
    """
    users = db.list_demo_profiles()
    return [
        {
            "user_id": u.user_id,
            "display_name": u.display_name or u.user_id,
            "persona": u.persona or "",
            "avatar": u.avatar or "🙂",
            "education_level": u.education_level.value,
            "grade": u.grade.value,
            "major": u.major,
            "skills": u.skills,
            "experiences": u.experiences,
            "weekly_available_hours": u.weekly_available_hours,
            "expected_team_size": u.expected_team_size,
        }
        for u in users
    ]


@app.get("/api/users/{user_id}/profile", response_model=UserProfile, tags=["用户"])
def get_profile(user_id: str) -> UserProfile:
    """获取用户画像（来自数据库）。"""
    profile = db.get_user_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return profile


@app.get("/api/profile/options", tags=["用户"])
def profile_options() -> dict:
    return {"grades_by_education": {level.value: [grade.value for grade in grades_for_education(level)]
                                    for level in EducationLevel}}


@app.get("/api/users/{user_id}/competitions/{competition_id}/eligibility", tags=["推荐"])
def competition_eligibility(user_id: str, competition_id: str) -> dict:
    profile = db.get_user_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="请先保存个人画像")
    competition = db.get_competition(competition_id)
    if competition is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    readiness = assess_recommendation_readiness(competition, contest_now())
    if not readiness.ready:
        return {"can_create_project": False, "eligible": None, "reasons": list(readiness.reasons)}
    eligible, reasons = eligibility_gate(profile, competition, contest_now())
    return {"can_create_project": eligible, "eligible": eligible, "reasons": [item.reason for item in reasons]}


@app.post("/api/users/{user_id}/profile", response_model=UserProfile, tags=["用户"])
def save_profile(user_id: str, profile: UserProfile) -> UserProfile:
    """保存用户画像；未勾选隐私授权时拒绝保存个性化数据。写入真正持久化。"""
    if user_id != profile.user_id:
        raise HTTPException(status_code=400, detail="路径 user_id 与请求体不一致")
    if profile.grade not in grades_for_education(profile.education_level):
        raise HTTPException(status_code=422, detail="请选择与学历层次相符的真实年级")
    if not profile.can_store_profile():
        raise HTTPException(
            status_code=403,
            detail="未勾选隐私授权，仅可浏览公开赛事，不能保存个性化画像",
        )
    return db.save_user_profile(profile)


@app.delete("/api/users/{user_id}/profile", tags=["用户"])
def delete_profile(user_id: str) -> dict:
    """删除个人画像及其项目数据，不删除任何赛事或官方证据。"""
    result = db.delete_user_profile(user_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return {"user_id": user_id, **result}


@app.get(
    "/api/users/{user_id}/recommendations",
    response_model=list[RecommendationResult],
    tags=["推荐"],
)
def recommendations(user_id: str) -> list[RecommendationResult]:
    """只返回通过可信门控并完成评分的正式推荐。"""
    profile = db.get_user_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    comps = db.get_all_competitions()
    return [
        item
        for item in recommend_for_user(profile, comps, contest_now())
        if item.eligible and item.score is not None
    ]


@app.post(
    "/api/users/{user_id}/portfolio/optimize",
    response_model=PortfolioOptimizeResponse,
    tags=["组合规划"],
)
def optimize_user_portfolio(
    user_id: str, preferences: PortfolioPreferences
) -> PortfolioOptimizeResponse:
    """在可信、资格和每周时间约束下生成稳妥/均衡/冲刺三套方案。"""
    profile = db.get_user_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    if not profile.privacy_consent:
        raise HTTPException(status_code=403, detail="未授权使用画像，不能进行组合规划")
    return optimize_portfolios(
        profile,
        db.get_all_competitions(),
        db.list_user_projects(user_id),
        preferences,
        contest_now(),
    )


@app.get("/api/users/{user_id}/portfolio/map", tags=["组合规划"])
def portfolio_map(user_id: str) -> dict:
    """返回作战地图：候选路线 + 已有项目风险节点 + 雷达影响边。"""
    profile = db.get_user_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    if not profile.privacy_consent:
        raise HTTPException(status_code=403, detail="未授权使用画像，不能生成作战地图")
    preferences = PortfolioPreferences()
    optimized = optimize_portfolios(
        profile,
        db.get_all_competitions(),
        db.list_user_projects(user_id),
        preferences,
        contest_now(),
    )
    projects = db.list_user_projects(user_id)
    alerts = db.list_user_alerts(user_id)
    project_maps = []
    for project in projects:
        nodes = [
            {
                "node_id": f"task:{item.item_id}",
                "node_type": "milestone" if item.phase in {"qualification", "submission", "defense"} else "material" if item.item_type.value == "material" else "competition",
                "label": item.title,
                "status": "blocked" if item.is_blocked else "done" if item.status.value in {"done", "skipped"} else "in_progress" if item.status.value == "in_progress" else "not_ready",
                "due_date": item.due_date.isoformat() if item.due_date else None,
                "competition_id": project.competition_id,
                "reason": item.blocked_reason,
            }
            for item in project.items
        ]
        edges = [
            {"source": f"task:{item.depends_on_item_id}", "target": f"task:{item.item_id}", "relation": "precedes"}
            for item in project.items if item.depends_on_item_id
        ]
        project_maps.append({
            "project_id": project.project_id,
            "competition_id": project.competition_id,
            "competition_name": project.competition_name,
            "risk_level": project.risk_level,
            "progress_percent": project.progress_percent,
            "nodes": nodes,
            "edges": edges,
        })
    return {
        "generated_at": optimized.generated_at,
        "routes": [plan.model_dump(mode="json") for plan in optimized.plans],
        "active_projects": project_maps,
        "pending_alerts": [item for item in alerts if item.get("status") == "pending"],
        "legend": {
            "not_ready": "未准备",
            "in_progress": "进行中",
            "done": "已完成",
            "blocked": "受官方变化或依赖阻塞",
        },
    }


@app.post("/api/users/{user_id}/portfolio/apply", tags=["组合规划"])
def apply_user_portfolio(user_id: str, payload: PortfolioApplyRequest) -> dict:
    """在单一事务中重新校验并幂等采用组合；任一失败则全部回滚。"""
    ids = list(dict.fromkeys(payload.competition_ids))
    if len(ids) != len(payload.competition_ids):
        raise HTTPException(status_code=400, detail="组合中存在重复赛事")
    try:
        created, failures = db.create_user_projects_atomic(user_id, ids, contest_now())
    except ValueError as exc:
        if str(exc) == "profile_required":
            raise HTTPException(status_code=404, detail="用户不存在") from exc
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if failures:
        raise HTTPException(
            status_code=409,
            detail={"message": "赛事状态已变化，请重新优化", "failures": failures},
        )
    return {"count": len(created), "projects": [item.model_dump(mode="json") for item in created]}


@app.get("/api/users/{user_id}/teammates", tags=["队友推荐"])
def user_teammates(user_id: str, top_k: int = Query(5, ge=1, le=20)) -> dict:
    """基于画像，从队友库中推荐互补搭档（按互补度降序）。"""
    seeker = db.get_user_profile(user_id)
    if seeker is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    candidates = db.list_demo_profiles(exclude_user_id=user_id)
    matches = recommend_teammates(seeker, candidates, top_k=top_k)
    return {
        "user_id": user_id,
        "count": len(matches),
        "demo_only": True,
        "matches": [m.model_dump(mode="json") for m in matches],
    }


# ---------------------------------------------------------------------------
# 认证：注册 / 登录 / 测试账号学生类型
# ---------------------------------------------------------------------------
class AuthRegisterPayload(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=64)
    display_name: str | None = Field(None, max_length=32)


class AuthLoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register", tags=["认证"])
def auth_register(payload: AuthRegisterPayload) -> dict:
    """注册新账号（用户名仅限字母 / 数字 / 下划线，3-32 位）。"""
    username = (payload.username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise HTTPException(status_code=400, detail="用户名仅限字母、数字、下划线（3-32 位）")
    ok = db.create_auth_user(username, payload.password, is_test=False, display_name=payload.display_name)
    if not ok:
        raise HTTPException(status_code=409, detail="用户名已存在")
    return {"username": username, "is_test": False, "display_name": payload.display_name or username}


@app.post("/api/auth/login", tags=["认证"])
def auth_login(payload: AuthLoginPayload) -> dict:
    """用户名 + 密码登录；返回账号信息与展示字段。"""
    username = (payload.username or "").strip()
    rec = db.get_auth_user(username)
    if rec and rec.get("is_test") and not demo_login_enabled():
        raise HTTPException(status_code=403, detail="当前部署未开放演示账号，请使用个人账号")
    if rec is None or not db.verify_password(payload.password, rec["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    display_name = rec.get("display_name") or username
    persona = ""
    avatar = None
    try:
        p = db.get_user_profile(username)
        if p:
            display_name = p.display_name or display_name
            persona = p.persona or ""
            avatar = p.avatar
    except Exception:
        pass
    return {
        "username": username,
        "is_test": rec["is_test"],
        "display_name": display_name,
        "persona": persona,
        "avatar": avatar,
        "access_token": _issue_access_token(username),
        "token_type": "bearer",
        "expires_in": _AUTH_TOKEN_TTL_SECONDS,
    }


@app.get("/api/student-types", tags=["认证"])
def student_types() -> list[dict]:
    """测试账号可切换的学生类型（取自演示画像库，用于预览「千人千面」）。"""
    users = db.list_demo_profiles()
    return [
        {
            "user_id": u.user_id,
            "display_name": u.display_name or u.user_id,
            "persona": u.persona or "",
            "avatar": u.avatar or "🙂",
            "education_level": u.education_level.value,
            "grade": u.grade.value,
            "major": u.major,
            "skills": u.skills,
            "experiences": u.experiences,
            "weekly_available_hours": u.weekly_available_hours,
            "expected_team_size": u.expected_team_size,
        }
        for u in users
    ]


# ---------------------------------------------------------------------------
# 我的项目：任务计划、材料清单、完成状态、ICS
# ---------------------------------------------------------------------------
@app.get("/api/users/{user_id}/projects", response_model=list[UserProject], tags=["我的项目"])
def list_projects(user_id: str) -> list[UserProject]:
    if db.get_user_profile(user_id) is None:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return db.list_user_projects(user_id)


@app.post("/api/users/{user_id}/projects", response_model=UserProject, tags=["我的项目"])
def join_project(user_id: str, payload: ProjectCreate) -> UserProject:
    try:
        return db.create_user_project(user_id, payload.competition_id)
    except ValueError as exc:
        errors = {
            "profile_required": (404, "请先保存个人画像"),
            "competition_not_found": (404, "赛事不存在"),
            "competition_basic_info_incomplete": (409, "赛事官方来源或关键证据不完整，仅作为候选信息，暂不能新建项目"),
            "competition_expired": (409, "赛事报名已经截止，不能新建参赛项目"),
            "profile_ineligible": (409, "你的学籍或团队条件不符合赛事要求，请先确认画像；暂不能创建参赛项目"),
        }
        status, message = errors.get(str(exc), (400, "无法加入项目"))
        raise HTTPException(status_code=status, detail=message)


@app.patch("/api/users/{user_id}/projects/{project_id}", response_model=UserProject, tags=["我的项目"])
def update_project(user_id: str, project_id: int, payload: ProjectUpdate) -> UserProject:
    project = db.update_user_project(user_id, project_id, payload.status)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@app.delete("/api/users/{user_id}/projects/{project_id}", tags=["我的项目"])
def delete_project(user_id: str, project_id: int) -> dict:
    if not db.delete_user_project(user_id, project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"deleted": True, "project_id": project_id}


@app.post(
    "/api/users/{user_id}/projects/{project_id}/items",
    response_model=ProjectItem,
    tags=["我的项目"],
)
def add_project_item(user_id: str, project_id: int, payload: ProjectItemCreate) -> ProjectItem:
    try:
        item = db.add_project_item(
            user_id,
            project_id,
            payload.item_type,
            payload.title,
            payload.due_date,
            phase=payload.phase,
            depends_on_item_id=payload.depends_on_item_id,
            blocked_reason=payload.blocked_reason,
            source_alert_id=payload.source_alert_id,
            source_citation_id=payload.source_citation_id,
            estimated_hours=payload.estimated_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return item


@app.patch(
    "/api/users/{user_id}/projects/{project_id}/items/{item_id}",
    response_model=ProjectItem,
    tags=["我的项目"],
)
def update_project_item(
    user_id: str, project_id: int, item_id: int, payload: ProjectItemUpdate
) -> ProjectItem:
    try:
        item = db.update_project_item(
            user_id,
            project_id,
            item_id,
            title=payload.title,
            due_date=payload.due_date,
            status=payload.status,
            phase=payload.phase,
            depends_on_item_id=payload.depends_on_item_id,
            blocked_reason=payload.blocked_reason,
            estimated_hours=payload.estimated_hours,
        )
    except ValueError as exc:
        messages = {
            "dependency_open": "前置任务尚未完成，当前任务仍处于阻塞状态",
            "dependency_cycle": "任务依赖不能形成循环",
            "dependency_not_in_project": "前置任务不属于当前项目",
        }
        raise HTTPException(status_code=409, detail=messages.get(str(exc), str(exc)))
    if item is None:
        raise HTTPException(status_code=404, detail="任务或材料不存在")
    return item


@app.post(
    "/api/users/{user_id}/tasks/from-agent",
    response_model=ProjectItem,
    tags=["我的项目"],
)
def task_from_agent(user_id: str, payload: AgentTaskCreate) -> ProjectItem:
    try:
        return db.create_task_from_agent(
            user_id,
            payload.competition_id,
            payload.title,
            payload.due_date,
            payload.source_citation_id,
            payload.estimated_hours,
        )
    except ValueError as exc:
        messages = {
            "profile_required": "请先保存个人画像",
            "competition_not_found": "赛事不存在",
            "competition_not_ready": "赛事当前不满足正式执行条件",
            "profile_ineligible": "你的学籍或团队条件不符合赛事要求，暂不能创建执行任务",
            "citation_not_in_project_competition": "引用与目标赛事不匹配",
        }
        raise HTTPException(status_code=409, detail=messages.get(str(exc), str(exc)))


@app.delete("/api/users/{user_id}/projects/{project_id}/items/{item_id}", tags=["我的项目"])
def delete_project_item(user_id: str, project_id: int, item_id: int) -> dict:
    if not db.delete_project_item(user_id, project_id, item_id):
        raise HTTPException(status_code=404, detail="任务或材料不存在")
    return {"deleted": True, "item_id": item_id}


def _ics_escape(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold_line(line: str) -> list[str]:
    """RFC 5545: fold at 75 UTF-8 octets without splitting a character."""
    parts = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current, size = " ", 1
        current += char
        size += width
    parts.append(current)
    return parts


@app.get("/api/users/{user_id}/projects/{project_id}/calendar.ics", tags=["我的项目"])
def export_project_calendar(user_id: str, project_id: int) -> Response:
    project = db.get_user_project(user_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    events: list[tuple[str, date | datetime, str]] = []
    timestamp_note = (
        "准确时刻来自官方原文，时区以官方依据为准"
        if project.deadline_timezone_basis == "official"
        else "准确时刻来自官方原文，时区使用校园UTC+08:00假设；请向官网确认"
    )
    if project.registration_deadline:
        events.append(("赛事报名截止", project.registration_deadline_at or project.registration_deadline, timestamp_note if project.registration_deadline_at else "官网仅提供日期，准确时刻待确认；请以最新官网为准"))
    if project.submission_deadline:
        events.append(("作品提交截止", project.submission_deadline_at or project.submission_deadline, timestamp_note if project.submission_deadline_at else "官网仅提供日期，准确时刻待确认；请以最新官网为准"))
    for item in project.items:
        if item.due_date:
            events.append((item.title, item.due_date, "项目计划日期，不等于官方截止时刻"))

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Campus Innovation Agent//CN", "CALSCALE:GREGORIAN"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for index, (title, event_date, description) in enumerate(events):
        if isinstance(event_date, datetime):
            start = event_date.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            date_lines = [f"DTSTART:{start}", "DURATION:PT1M"]
        else:
            start = event_date.strftime("%Y%m%d")
            end = (event_date + timedelta(days=1)).strftime("%Y%m%d")
            date_lines = [f"DTSTART;VALUE=DATE:{start}", f"DTEND;VALUE=DATE:{end}"]
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:project-{project_id}-{index}@campus-agent",
            f"DTSTAMP:{stamp}",
            *date_lines,
            f"SUMMARY:{_ics_escape(project.competition_name + ' · ' + title)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    content = "\r\n".join(part for line in lines for part in _ics_fold_line(line)) + "\r\n"
    return Response(
        content=content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="project-{project_id}.ics"'},
    )


# ---------------------------------------------------------------------------
# 赛事隔离 RAG 检索
# ---------------------------------------------------------------------------
@app.get("/api/rag/search", response_model=list[Citation], tags=["RAG"])
def rag_search(
    competition_id: str = Query(..., description="必填：限定检索的赛事，强制隔离"),
    q: str = Query(..., description="自然语言问题，如「团队人数要求」"),
    year: int | None = Query(None, description="可选：文档年份过滤，避免跨年混用"),
    doc_version: str | None = Query(None, description="可选：文档版本过滤"),
    top_k: int = Query(3, ge=1, le=10, description="返回条数"),
) -> list[Citation]:
    """在指定赛事范围内做隔离检索，返回带页码/原文/官方链接的引用证据。

    检索范围被 competition_id 强制隔离（独立集合 + metadata 三重过滤），
    不会串入其他赛事或其他年份的规则。
    """
    if db.get_competition(competition_id) is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return get_rag().query(competition_id, q, document_year=year, doc_version=doc_version, top_k=top_k)


@app.post("/api/rag/reseed", tags=["RAG"], dependencies=[Depends(_require_admin_token)])
def rag_reseed() -> dict:
    """从数据库重新灌库（幂等，按 competition_id 覆盖）。人工修订标注后可调用。"""
    stats = get_rag().seed_from_db()
    return {
        "competitions": len(stats),
        "chunks": sum(stats.values()),
        "embedding_backend": embedding_backend(),
        "detail": stats,
    }


@app.post("/api/competitions/{competition_id}/ingest", tags=["RAG"], dependencies=[Depends(_require_admin_token)])
def rag_ingest_one(competition_id: str) -> dict:
    """把单个赛事重新灌入其隔离集合（人工修订某赛事后局部刷新）。"""
    comp = db.get_competition(competition_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    n = get_rag().ingest_competition(comp)
    return {"competition_id": competition_id, "chunks": n}


# ---------------------------------------------------------------------------
# LangGraph Agent 闭环（意图路由 → 隔离检索 → 门控 → 评分 → 组队文案）
# ---------------------------------------------------------------------------
@app.get("/api/agent/ask", tags=["Agent"])
def agent_ask(
    question: str = Query(..., description="用户自然语言问题"),
    user_id: str | None = Query(None, description="用户ID，用于画像驱动的推荐/门控/文案"),
    competition_id: str | None = Query(None, description="可选：前端直接指定目标赛事，否则由路由自动锁定"),
    context_competition_id: str | None = Query(None, description="当前对话上一轮赛事，仅用于自然追问"),
    top_k: int = Query(4, ge=1, le=10, description="隔离检索返回条数"),
    model: str | None = Query(None, description="可选：请求级覆盖默认 LLM 模型（如 qwen-plus/qwen-max/qwen-turbo），仅同源 key 可用"),
    task_mode: bool = Query(False, description="按参赛目标执行受限工具规划与结果核验"),
) -> dict:
    """走完整 Agent 闭环，返回意图、答案、引用证据、门控/评分/推荐结果、执行轨迹。

    问答自动带引用：答案文本内嵌 [1][2]… 角标，citations 提供可点击来源。
    """
    from agent.llm import configured_models
    if model and model not in configured_models():
        raise HTTPException(status_code=400, detail="所选模型未由服务端配置，请刷新模型列表")
    try:
        result = run_agent(question, user_id=user_id, competition_id=competition_id, top_k=top_k,
                           model=model, context_competition_id=context_competition_id, task_mode=task_mode)
        result["user_id"] = user_id
        db.save_agent_run(result)
        return result
    except Exception as exc:  # 闭环异常不应崩服务
        raise HTTPException(status_code=500, detail="Agent 执行未完成，请稍后重试")


@app.get("/api/agent/llm-status", tags=["Agent"])
def agent_llm_status() -> dict:
    """返回 LLM 润色层 + 联网搜索层的启用状态，供前端展示能力指示。"""
    from agent.llm import is_llm_enabled, configured_models
    from agent.web_search import is_web_search_enabled

    return {
        "enabled": is_llm_enabled(),
        "model": os.getenv("AGENT_LLM_MODEL") or "gpt-4o-mini",
        "models": configured_models(),
        "provider": os.getenv("AGENT_LLM_PROVIDER") or "",
        "web_search_enabled": is_web_search_enabled(),
        "web_search_provider": (os.getenv("WEB_SEARCH_PROVIDER") or "duckduckgo") if is_web_search_enabled() else "",
    }


@app.get("/api/users/{user_id}/agent/runs", tags=["Agent"])
def agent_run_history(user_id: str, limit: int = Query(20, ge=1, le=100)) -> dict:
    if db.get_user_profile(user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    runs = db.list_agent_runs(user_id, limit)
    return {"count": len(runs), "runs": runs}


@app.post("/api/agent/runs/{run_id}/replay", tags=["Agent"])
def replay_agent_run(run_id: str) -> dict:
    saved = db.get_agent_run(run_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    result = run_agent(
        saved["question"],
        user_id=saved.get("user_id"),
        competition_id=saved.get("resolved_competition"),
        task_mode=saved.get("intent") == "plan",
    )
    result["replayed_from"] = run_id
    result["user_id"] = saved.get("user_id")
    db.save_agent_run(result)
    return result


# ---------------------------------------------------------------------------
# 数据维护闭环（上传 → 解析 → 来源确认 → 入库）
# ---------------------------------------------------------------------------
@app.post("/api/auth/dev-admin-login", tags=["认证"], include_in_schema=False)
def dev_admin_login(request: Request) -> dict:
    """仅供本机调试的一键管理员入口；必须显式设置开关，线上默认不存在。"""
    client_host = request.client.host if request.client else ""
    local_client = client_host in {"127.0.0.1", "::1", "localhost"}
    enabled = os.getenv("DEV_ADMIN_QUICK_LOGIN", "").strip().lower() in {"1", "true", "yes"}
    if not enabled or not local_client or is_production():
        raise HTTPException(status_code=404, detail="本地调试入口未启用")
    if not _ADMIN_API_TOKEN:
        raise HTTPException(status_code=503, detail="本地管理员令牌未配置")
    # 演示账号由 init_db 的种子数据创建；不在此处写入或提升任意真实用户。
    payload = AuthLoginPayload(username="test", password="test123")
    response = auth_login(payload)
    response["admin_token"] = _ADMIN_API_TOKEN
    response["dev_only"] = True
    return response

@app.get("/api/radar/status", tags=["赛事雷达"])
def radar_status() -> dict:
    """公开展示雷达运行状态；待审核变化明确标注，不作为正式事实。"""
    watches = db.list_source_watches()
    assessed = [item for item in watches if item.get("health_status") not in {"unknown", "paused"}]
    events = db.list_change_events(limit=20)
    public_watches = [
        {key: value for key, value in item.items() if key not in {"etag", "last_modified", "last_error"}}
        for item in watches
    ]
    return {
        "watch_count": len(watches),
        "healthy_count": sum(1 for item in watches if item.get("health_status") == "healthy"),
        "pending_count": sum(1 for item in events if item["status"] == "pending"),
        "average_health_score": round(
            sum(item.get("health_score", 0) for item in assessed) / len(assessed), 1
        ) if assessed else None,
        "next_scan_at": min(
            (item["next_scan_at"] for item in watches if item.get("next_scan_at")),
            default=None,
        ),
        "watches": public_watches,
        "events": events,
    }


@app.get("/api/users/{user_id}/alerts", tags=["赛事雷达"])
def user_alerts(user_id: str) -> dict:
    if db.get_user_profile(user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    items = db.list_user_alerts(user_id)
    return {
        "count": len(items),
        "pending_count": sum(1 for item in items if item.get("status") == "pending"),
        "alerts": items,
    }


@app.post("/api/users/{user_id}/alerts/{alert_id}/action", tags=["赛事雷达"])
def user_alert_action(user_id: str, alert_id: int, payload: RadarAlertAction) -> dict:
    try:
        result = db.act_on_user_alert(user_id, alert_id, payload.action, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="收件箱事项不存在")
    return result


@app.post("/api/admin/verify", tags=["数据维护"])
def admin_verify(_: None = Depends(_require_admin_token)) -> dict:
    """校验管理员令牌；前端据此决定是否显示「数据维护」入口。"""
    return {"ok": True}


@app.get("/api/admin/radar/watches", tags=["赛事雷达"])
def admin_radar_watches(_: None = Depends(_require_admin_token)) -> dict:
    items = db.list_source_watches()
    return {"count": len(items), "watches": items}


@app.post("/api/admin/radar/watches", tags=["赛事雷达"])
def admin_create_radar_watch(
    payload: RadarWatchCreate, _: None = Depends(_require_admin_token)
) -> dict:
    try:
        validate_public_url(payload.source_url)
        validate_watch_configuration(
            payload.include_selector or payload.css_selector,
            payload.exclude_selector,
            payload.ignore_regex,
            payload.trigger_terms,
        )
        return db.create_source_watch(**payload.model_dump())
    except ValueError as exc:
        status = 404 if str(exc) == "competition_not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc))


@app.post("/api/admin/radar/run", tags=["赛事雷达"])
def admin_run_radar(payload: dict | None = None, _: None = Depends(_require_admin_token)) -> dict:
    payload = payload or {}
    watch_ids = payload.get("watch_ids")
    demo_content = payload.get("demo_content_by_watch")
    return run_radar(watch_ids=watch_ids, demo_content_by_watch=demo_content)


@app.get("/api/admin/radar/events", tags=["赛事雷达"])
def admin_radar_events(
    status: str | None = Query(None), _: None = Depends(_require_admin_token)
) -> dict:
    items = db.list_change_events(status=status)
    return {"count": len(items), "events": items}


@app.post("/api/admin/radar/events/{event_id}/review", tags=["赛事雷达"])
def admin_review_radar_event(
    event_id: int, payload: RadarEventReview, _: None = Depends(_require_admin_token)
) -> dict:
    item = db.review_change_event(event_id, payload.action, payload.note)
    if item is None:
        raise HTTPException(status_code=404, detail="变化事件不存在")
    if payload.action == "approve":
        try:
            get_rag().seed_from_db()
        except Exception as exc:
            print(f"[radar] 审核后刷新 RAG 失败（不影响事实更新）：{exc}")
    return item


def _classify_submitted_competition(comp: Competition) -> tuple[Competition, list[str]]:
    """Derive trust without inventing missing source metadata."""
    comp = comp.model_copy(deep=True)

    # Assess the strongest possible state; incomplete evidence is immediately downgraded.
    comp.data_status = DataStatus.VERIFIED
    comp.trusted_level = TrustedLevel.A
    assessment = assess_source_readiness(comp)
    if not assessment.ready:
        comp.data_status = DataStatus.UNVERIFIED
        comp.trusted_level = TrustedLevel.B if comp.official_source_status == "found" else TrustedLevel.C
    return comp, list(assessment.reasons)


class AdminUploadPayload(BaseModel):
    filename: str
    content_base64: str


@app.post("/api/admin/upload", tags=["数据维护"])
def admin_upload(payload: AdminUploadPayload, _: None = Depends(_require_admin_token)) -> dict:
    """上传官方通知 PDF/Word（前端 base64 上传，避免依赖 python-multipart），落盘到 data/uploads。"""
    ext = Path(payload.filename or "file.bin").suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="仅支持 PDF / Word(.docx) 文件")
    try:
        if len(payload.content_base64) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="文件超过 15MB 安全上限")
        raw = base64.b64decode(payload.content_base64, validate=True)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="文件内容 base64 解码失败")
    if not raw:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 15MB 安全上限")
    file_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    save_path = UPLOAD_DIR / (file_id + ext)
    save_path.write_bytes(raw)
    mime_type = "application/pdf" if ext == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    document = db.store_document(payload.filename, mime_type, raw)
    return {
        "file_id": file_id,
        "filename": payload.filename,
        "ext": ext,
        "saved_as": save_path.name,
        "document_id": document["document_id"],
        "sha256": document["sha256"],
    }


@app.post("/api/admin/parse", tags=["数据维护"])
def admin_parse(payload: dict, _: None = Depends(_require_admin_token)) -> dict:
    """解析已上传文件，返回带页码文本块 + 字段抽取建议（含证据块 index）。"""
    file_id = payload.get("file_id")
    if not file_id:
        raise HTTPException(status_code=400, detail="缺少 file_id，请先上传")
    if not isinstance(file_id, str) or not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", file_id):
        raise HTTPException(status_code=400, detail="file_id 格式无效")
    candidates = sorted(UPLOAD_DIR.glob(f"{file_id}.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail="上传文件不存在，请重新上传")
    path = candidates[0]
    try:
        cat_enum = CompetitionCategory(payload.get("category", "programming"))
    except ValueError:
        cat_enum = CompetitionCategory.PROGRAMMING
    document_year = int(payload.get("document_year", 2026) or 2026)
    document_id = payload.get("document_id")
    if document_id is not None and db.get_document(int(document_id)) is None:
        raise HTTPException(status_code=404, detail="上传文档记录不存在，请重新上传")
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            result = pdf_extractor.parse_pdf(
                path, cat_enum, document_year, competition_id=file_id, document_id=document_id
            )
        else:
            result = pdf_extractor.parse_docx(
                path, cat_enum, document_year, competition_id=file_id, document_id=document_id
            )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"解析失败：{exc}")
    blocks = [
        {
            "index": i,
            "page": b.page,
            "paragraph_index": b.paragraph_index,
            "text": b.text,
            "rects": [rect.model_dump() for rect in b.rects],
            "anchor_quality": b.anchor_quality,
        }
        for i, b in enumerate(result.blocks)
    ]
    suggestions = extract_suggest.suggest_fields(blocks)
    return {
        "document_name": result.document_name,
        "category": cat_enum.value,
        "document_year": document_year,
        "block_count": len(blocks),
        "blocks": blocks,
        "suggestions": suggestions,
        "document_id": result.document_id,
    }


@app.post("/api/admin/competitions", tags=["数据维护"])
def admin_confirm_competition(
    comp: Competition, _: None = Depends(_require_admin_token)
) -> dict:
    """关联官方来源后入库；可信等级由统一证据完整性规则自动决定。"""
    comp, readiness_reasons = _classify_submitted_competition(comp)
    db.upsert_competition(comp)
    try:
        n = get_rag().ingest_competition(comp)
    except Exception as exc:
        n = -1
        print(f"[rag] 入库后刷新隔离集合失败（不影响数据）：{exc}")
    return {
        "competition_id": comp.competition_id,
        "data_status": comp.data_status.value,
        "trusted_level": comp.trusted_level.value,
        "evidence_count": len(comp.evidence),
        "recommendation_ready": not readiness_reasons,
        "readiness_reasons": readiness_reasons,
        "rag_chunks": n,
    }


# ---------------------------------------------------------------------------
# 定时自动发现：批量入库（不强制 verified/A）
# ---------------------------------------------------------------------------
class AdminBulkIngestPayload(BaseModel):
    competitions: list[Competition]


@app.post("/api/admin/competitions/bulk", tags=["数据维护"])
def admin_bulk_ingest(
    payload: AdminBulkIngestPayload, _: None = Depends(_require_admin_token)
) -> dict:
    """批量自动入库；每条记录按官方来源证据重新计算可信状态。

    用于定时发现的新赛事：若库中已存在同 ID 且已通过统一来源规则，则跳过，避免自动
    流程覆盖推荐级证据。随后刷新对应赛事的隔离 RAG 集合（失败不影响数据）。
    """
    results: list[dict] = []
    for comp in payload.competitions:
        existing = db.get_competition(comp.competition_id)
        if existing is not None and assess_source_readiness(existing).ready:
            results.append({"competition_id": comp.competition_id, "status": "skipped_verified"})
            continue
        comp, readiness_reasons = _classify_submitted_competition(comp)
        db.upsert_competition(comp)
        try:
            n = get_rag().ingest_competition(comp)
        except Exception as exc:  # RAG 失败不应阻断主服务
            n = -1
            print(f"[rag] 批量入库后刷新隔离集合失败（不影响数据）：{exc}")
        results.append({
            "competition_id": comp.competition_id,
            "status": "upserted",
            "recommendation_ready": not readiness_reasons,
            "readiness_reasons": readiness_reasons,
            "rag_chunks": n,
        })
    return {"count": len(results), "results": results}


# ---------------------------------------------------------------------------
# 前端静态资源（同源部署，避免 CORS）
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", tags=["页面"])
def index() -> FileResponse:
    """前端单页应用入口。"""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
