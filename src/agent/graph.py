"""LangGraph Agent 闭环 —— 意图路由 → 隔离检索 → 门控 → 评分 → 组队文案。

设计目标（对应修订方案「亮点：Agent 编排」）：
  - 用 LangGraph StateGraph 把已有的四个能力模块串成一个可解释的闭环：
        route_intent   意图路由（判断用户想干什么、锁定目标赛事）
        retrieve       赛事隔离检索（rag.store，强制 competition_id 隔离）
        gate           硬性门控（recommendation.engine.eligibility_gate）
        score          软性评分（recommendation.engine.soft_match_score）
        team_copy      组队招募文案生成（基于赛事字段 + 检索证据）
        recommend_all  全库门控+评分推荐（recommend intent 专用）
        compose        组装最终答案，**自动附带引用证据**
  - 「让问答自动带引用」：任何回答都在 state["citations"] 中携带来源，
    compose 节点把引用编号嵌入答案文本（[1][2]…），前端可渲染角标。
  - 离线可运行：不依赖任何外部 LLM Key。意图路由用规则 + 最长公共子串
    锁定赛事；答案默认用确定性模板生成（LLM 未启用时的回退）。若配置了 LLM
    （设 AGENT_LLM_API_KEY，可选 AGENT_LLM_BASE_URL 指向任意 OpenAI 兼容端点）：
      * qa / detail（指向具体赛事的事实查询）：由 LLM 基于检索证据**自由撰写**
        答案（可解释、对比、给建议），但所有赛事事实必须带 [n] 引用、且不得
        超出证据范围（见 src/agent/skills/evidence_speaking.md 铁律 + 引用校验）。
      * chat（开放/建议/通用问题）：走自由对话，LLM 当参谋，同样受证据铁律约束。
      * recommend / team：保持确定性逻辑（列表/文案），team 文案做轻量润色。
    门控结论与引用编号始终来自本地可信数据，模型无法改写；生成失败自动回退模板。

执行轨迹：state["trace"] 记录结构化、可审计的业务步骤，前端可展示
「意图→检索→门控→评分→文案」；不记录提示词或模型隐藏思维过程。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Optional, TypedDict
from uuid import uuid4

# 允许脚本直接运行（python agent/graph.py）或模块运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 优先使用真实 langgraph；无法安装（如受限环境内存不足）时降级到内置等价引擎，
# 保证离线可运行、对外 API 与行为不变。
try:  # pragma: no cover - 依赖可选
    from langgraph.graph import END, START, StateGraph  # type: ignore
    _USING_LANGGRAPH = True
except Exception:  # noqa: BLE001
    from agent._graph_shim import END, START, StateGraph  # type: ignore
    _USING_LANGGRAPH = False

import db
from fact_formatting import format_deadline, format_team_size
from contest_clock import contest_now
from rag.store import get_rag
from recommendation.engine import (
    data_validity_check,
    eligibility_gate,
    recommend_for_user,
    recommend_teammates,
    soft_match_score,
)
from schemas import Citation, Competition, UserProfile
from trust import assess_recommendation_readiness, assess_source_readiness
from agent.llm import generate_answer as _llm_generate
from agent.web_search import web_search as _web_search, is_web_search_enabled as _web_enabled


# ---------------------------------------------------------------------------
# 状态定义
# ---------------------------------------------------------------------------


class AuditTraceStep(TypedDict):
    """可公开展示的业务审计步骤，不包含模型隐藏思维过程。"""

    node: str
    label: str
    status: str
    duration_ms: float
    evidence_count: int
    data_version: str
    detail: str
    fallback: bool


class AgentState(TypedDict, total=False):
    # 输入
    question: str
    run_id: str
    user_id: Optional[str]
    competition_id: Optional[str]     # 可由前端直接指定，否则路由自动锁定
    context_competition_id: Optional[str]
    top_k: int
    model: Optional[str]               # 可选：请求级覆盖默认 LLM 模型

    # 路由结果
    intent: str                       # qa | detail | recommend | team | teammate | chat | unknown
    resolved_competition: Optional[str]
    resolved_name: Optional[str]
    version_resolution_note: Optional[str]
    clarification: Optional[str]

    # 中间产物
    profile: Optional[dict]
    citations: list[dict]             # 序列化后的 Citation（供 API/前端）
    gate: dict
    score: dict
    recommendations: list[dict]
    team_copy: str
    teammate_matches: list[dict]      # 基于画像的互补队友推荐
    web_results: list[dict]

    # 输出
    answer: str
    pending_review: bool
    error: Optional[str]
    data_version: str
    # 节点内部仍可临时追加旧版字符串；节点包装器会在每步结束时统一收敛成
    # AuditTraceStep。这样既兼容现有节点实现，也保证 API 最终只返回结构化轨迹。
    trace: list[AuditTraceStep | str]


_TRACE_NODE_LABELS = {
    "route_intent": "意图识别与赛事锁定",
    "retrieve": "可信证据检索",
    "web_augment": "联网信息补充",
    "gate": "资格与来源门控",
    "score": "适配度评分",
    "team_copy": "组队文案生成",
    "teammate_match": "互补队友匹配",
    "recommend_all": "全库赛事推荐",
    "compose": "答案组装与引用校验",
}

_TRACE_DEFAULT_DETAILS = {
    "route_intent": "已完成意图识别与赛事版本解析",
    "retrieve": "已完成可信证据检索",
    "web_augment": "联网补充未触发或按当前策略跳过",
    "gate": "已完成资格与来源门控",
    "score": "已完成适配度评分",
    "team_copy": "已完成组队文案处理",
    "teammate_match": "已完成互补队友匹配",
    "recommend_all": "已完成全库赛事推荐",
    "compose": "已完成答案组装与引用校验",
}

_TRACE_FALLBACK_TERMS = (
    "兜底",
    "回退",
    "降级",
    "节点执行异常",
    "关键证据待补充",
    "官方来源尚待核验",
    "未启用或检索无结果",
    "歧义澄清",
)


def _catalog_data_version() -> str:
    """生成可复核但不泄露数据内容的赛事目录版本标识。"""
    try:
        competitions = db.get_all_competitions()
    except Exception:  # noqa: BLE001 - 版本标识失败不应阻断主链路
        return "catalog:unavailable"
    verified_values = [
        str(value)
        for comp in competitions
        for value in (getattr(comp, "last_verified_at", None),)
        if value
    ]
    latest = max(verified_values) if verified_values else "unverified"
    return f"catalog:{len(competitions)}@{latest}"


def _state_data_version(state: dict) -> str:
    """优先返回当前赛事文档版本，否则返回目录版本。"""
    competition_id = state.get("resolved_competition") or state.get("competition_id")
    if competition_id:
        try:
            comp = db.get_competition(competition_id)
        except Exception:  # noqa: BLE001 - 审计元数据不可影响业务结果
            comp = None
        if comp is not None:
            doc_version = getattr(comp, "doc_version", None) or str(comp.document_year)
            verified_at = (
                getattr(comp, "last_verified_at", None)
                or getattr(comp, "source_acquired_date", None)
                or "unverified"
            )
            return f"{comp.competition_id}:{doc_version}@{verified_at}"
    return str(state.get("data_version") or _catalog_data_version())


def _trace_detail(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("detail") or item.get("label") or item.get("node") or "")
    return str(item or "")


def _normalize_existing_trace(items: list[Any], state: dict) -> list[AuditTraceStep]:
    """把外部传入的旧版字符串轨迹同步适配为结构化步骤。"""
    normalized: list[AuditTraceStep] = []
    data_version = _state_data_version(state)
    for index, item in enumerate(items):
        if isinstance(item, dict) and all(
            key in item
            for key in (
                "node",
                "label",
                "status",
                "duration_ms",
                "evidence_count",
                "data_version",
                "detail",
                "fallback",
            )
        ):
            normalized.append(dict(item))  # type: ignore[arg-type]
            continue
        detail = _trace_detail(item).strip() or "旧版轨迹记录"
        normalized.append(
            {
                "node": f"legacy_{index + 1}",
                "label": "兼容轨迹",
                "status": "succeeded",
                "duration_ms": 0.0,
                "evidence_count": len(state.get("citations") or []),
                "data_version": data_version,
                "detail": detail[:1200],
                "fallback": any(term in detail for term in _TRACE_FALLBACK_TERMS),
            }
        )
    return normalized


def _safe_node_fallback(node_name: str, state: AgentState, exc: Exception) -> AgentState:
    """节点异常时返回最小、保守且可继续编排的状态。"""
    error_code = f"{node_name}:{exc.__class__.__name__}"
    update: AgentState = {**state, "error": error_code, "pending_review": True}
    if node_name == "route_intent":
        update.update(
            intent="unknown",
            resolved_competition=None,
            resolved_name=None,
            clarification=None,
        )
    elif node_name == "retrieve":
        update["citations"] = []
    elif node_name == "web_augment":
        update["web_results"] = []
    elif node_name == "gate":
        update["gate"] = {"skipped": True, "reason": "节点异常，已保守跳过"}
    elif node_name == "score":
        update["score"] = {"skipped": True, "reason": "节点异常，已保守跳过"}
    elif node_name == "team_copy":
        update["team_copy"] = ""
    elif node_name == "teammate_match":
        update["teammate_matches"] = []
    elif node_name == "recommend_all":
        update["recommendations"] = []
    elif node_name == "compose":
        update["answer"] = "本次处理未能完整完成，系统已进入保守兜底；请稍后重试或查看官方来源。"
    return update


def _instrument_node(
    node_name: str,
    func: Callable[[AgentState], AgentState],
) -> Callable[[AgentState], AgentState]:
    """为业务节点补充结构化审计轨迹和异常兜底。

    ``detail`` 只汇总节点本来就会公开的业务结果，例如“命中 3 条证据”或
    “资格门控通过”；不会采集提示词、隐藏推理或模型思维链。
    """

    label = _TRACE_NODE_LABELS[node_name]

    def wrapped(state: AgentState) -> AgentState:
        started = perf_counter()
        before_raw = list(state.get("trace") or [])
        before = _normalize_existing_trace(before_raw, state)
        try:
            result = func({**state, "trace": before})
            if not isinstance(result, dict):
                result = dict(state)
            raw_trace = list(result.get("trace") or before)
            emitted = raw_trace[len(before):] if len(raw_trace) >= len(before) else raw_trace
            messages = [message for message in (_trace_detail(item).strip() for item in emitted) if message]
            detail = "；".join(messages) or _TRACE_DEFAULT_DETAILS[node_name]
            fallback = any(term in detail for term in _TRACE_FALLBACK_TERMS)
            if "跳过" in detail or (node_name == "web_augment" and not messages):
                status = "skipped"
            elif fallback or "待核验" in detail or "待确认" in detail:
                status = "degraded"
            else:
                status = "succeeded"
        except Exception as exc:  # noqa: BLE001 - 节点级保守兜底，错误码会返回调用方
            result = _safe_node_fallback(node_name, state, exc)
            detail = f"{label}：节点执行异常，已进入保守兜底（{exc.__class__.__name__}）"
            fallback = True
            status = "failed"

        data_version = _state_data_version(result)
        step: AuditTraceStep = {
            "node": node_name,
            "label": label,
            "status": status,
            "duration_ms": round(max(0.0, (perf_counter() - started) * 1000), 3),
            "evidence_count": len(result.get("citations") or []),
            "data_version": data_version,
            "detail": detail[:1200],
            "fallback": fallback,
        }
        return {
            **result,
            "data_version": data_version,
            "trace": [*before, step],
        }

    wrapped.__name__ = f"audited_{node_name}"
    wrapped.__doc__ = func.__doc__
    return wrapped


# ---------------------------------------------------------------------------
# 工具：赛事锁定（最长公共子串 + 别名）
# ---------------------------------------------------------------------------

_GENERIC_TOKENS = ["大赛", "竞赛", "大学生", "全国", "中国", "比赛", "杯", "赛"]

# 口语别名 → 库内正式名称。CHALLENGE_PROBE 发现「互联网+」因库名已改为
# 「中国国际大学生创新大赛」而 LCS 抓不到，落到通用 chat 无引用（c15）。
# 这里在做赛事锁定时把别名展开为正式名，使指定具体赛事的开放问法统一带 [n] 引用。
_NAME_ALIASES = {
    "互联网+": "中国国际大学生创新大赛",
    "互联网加大赛": "中国国际大学生创新大赛",
    "互联网+大赛": "中国国际大学生创新大赛",
    "互联网大学生创新创业大赛": "中国国际大学生创新大赛",
    "中国国际大学生创新大赛": "中国国际大学生创新大赛",
}


def _alias_expand(q: str) -> str:
    """把口语别名并入查询串，使「互联网+」等能命中库内正式名。

    赛事锁定与开放对话检索都要用，保证指定具体赛事的问法统一带 [n] 引用。
    """
    q = _norm(q)
    for _alias, _canonical in _NAME_ALIASES.items():
        if _alias in q:
            q = (q + " " + _canonical).strip()
            break
    return q


def _norm(s: str) -> str:
    """归一化：全角→半角、空白压缩，让「互联网＋」与「互联网+」可匹配。"""
    if not s:
        return ""
    s = s.replace("＋", "+").replace("—", "-").replace("，", ",").replace("。", ".")
    return " ".join(s.split())


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（用于赛事名模糊匹配）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _strip_year(s: str) -> str:
    """去掉年份（如「2026年」），避免赛事名里的年份前缀与问题里的年份形成
    虚假公共子串，导致「2026年蓝桥杯」被错配到「2026年XX大赛」。年份消歧
    交给 ``_resolve_competition_versioned`` 的 ``mentioned_years`` 处理。"""
    return re.sub(r"20\d{2}", "", s).replace("年", "")


def _competition_match_score(question: str, competition: Competition) -> float:
    q = _norm(question)
    c = competition
    name = c.competition_name or ""
    core = name
    for token in _GENERIC_TOKENS:
        core = core.replace(token, "")
    core = "".join(ch for ch in core if not ch.isdigit()).strip()
    # 完整名 / 核心名 / ID 直接命中仍保留（强信号）
    if name and name in q:
        return 10 + len(name)
    if core and core in q:
        return 8 + len(core)
    if c.competition_id and c.competition_id in q:
        return 6
    # 模糊匹配时对「完整名」剥离年份再比 LCS，既保留「蓝桥杯」等品牌字，
    # 又避免赛事名里的「2026年」前缀与问题里的年份形成虚假公共子串。
    lcs = _lcs_len(_strip_year(q), _strip_year(name)) if name else 0
    brands = ("蓝桥杯", "挑战杯", "三创赛", "美赛", "国赛")
    brand_match = any(brand in q and brand in name for brand in brands)
    return 3 + lcs if lcs >= 4 or brand_match else 0


def _resolve_competition_versioned(
    question: str, comps: list[Competition]
) -> tuple[Optional[Competition], Optional[str], Optional[str]]:
    """锁定赛事并显式处理同名多年份版本。"""
    q = _norm(question)
    year = contest_now().year
    q = q.replace("今年", str(year)).replace("明年", str(year + 1)).replace("去年", str(year - 1))
    # 别名展开：把口语别名并入匹配查询，使「互联网+」等能命中库内正式名。
    q_match = _alias_expand(q)
    def _implicit_version_result(selected: Competition, pool: list[Competition]):
        requested_years = {int(y) for y in re.findall(r"20\d{2}", q)}
        if len(requested_years) > 1:
            return None, None, "你提到了多个年份，请明确本次要查询的年份与赛事版本。"
        if requested_years:
            matches = [item for item in pool if item.document_year in requested_years]
            if not matches:
                return None, None, "库中没有你指定年份的该赛事通知，请确认年份或查看官网最新公告。"
            if len(matches) > 1:
                return None, None, "该年份存在多个赛道或通知版本，请补充赛道名称或赛事 ID：" + "、".join(item.competition_id for item in matches)
            return max(matches, key=lambda item: item.document_year), None, None
        years = {item.document_year for item in pool}
        if not re.search(r"20\d{2}", q) and len(years) > 1:
            ready = [item for item in pool if assess_source_readiness(item).ready]
            if not ready:
                return None, None, "该赛事存在多个年份，暂无官网来源与关键证据完整的版本。请明确年份后再查询。"
            selected = max(ready, key=lambda item: item.document_year)
            if sum(item.document_year == selected.document_year for item in ready) > 1:
                return None, None, "最新官网来源已确认年份存在多个赛道或通知版本，请补充赛道名称或赛事 ID。"
            note = f"你没有指定年份，以下使用最新官网来源已确认版本：{selected.document_year}年（{selected.doc_version}）。"
            return selected, note, None
        if len(pool) > 1:
            return None, None, "该赛事存在多个赛道或通知版本，请补充赛道名称或赛事 ID：" + "、".join(item.competition_id for item in pool)
        return selected, None, None
    id_matches = [item for item in comps if item.competition_id and item.competition_id in q]
    if id_matches:
        longest = max(len(item.competition_id) for item in id_matches)
        matches = [item for item in id_matches if len(item.competition_id) == longest]
        return _implicit_version_result(matches[0], matches)
    # Full official names take precedence over broad brand/topic heuristics.
    exact = [item for item in comps if _norm(item.competition_name) in q_match]
    if exact:
        longest = max(len(_norm(item.competition_name)) for item in exact)
        exact = [item for item in exact if len(_norm(item.competition_name)) == longest]
        names = {_norm(item.competition_name) for item in exact}
        if len(names) > 1:
            return None, None, "你提到了多个赛事，请明确本次要查询的赛事名称。"
        return _implicit_version_result(exact[0], exact)
    if "mathorcup" in q.lower():
        pool = [c for c in comps if "mathorcup" in c.competition_name.lower()]
        if "春季" in q and "大数据" in q:
            return None, None, "MathorCup春季赛与大数据竞赛是独立赛项，请分别指定赛事和年份查询截止日期。"
        if "大数据" in q:
            pool = [c for c in pool if "大数据" in c.competition_name]
        elif "春季" in q:
            pool = [c for c in pool if "大数据" not in c.competition_name]
        if pool:
            return _implicit_version_result(pool[0], pool)
    # 建模类问法显式纠正：避免「全国大学生数学竞赛（CMC）」因名称含「数学」而被抢锁，
    # 导致用户问「数学建模怎么备赛」时路由到数学竞赛、起手纠结「你问的是哪个」，答非所问。
    if "数学建模" in q:
        _model_pool = [c for c in comps if getattr(c, "category", None) == "modeling"
                       and "数学建模" in (c.competition_name or "")]
        if _model_pool:
            if any(t in q for t in ["美赛", "mcm", "icm", "美国", "comap"]):
                _us = [c for c in _model_pool if "美国" in (c.competition_name or "")
                       or "MCM" in (c.competition_name or "").upper()]
                if _us:
                    selected = max(_us, key=lambda c: c.document_year)
                    return _implicit_version_result(selected, _us)
            else:
                _cn = [c for c in _model_pool if "国赛" in (c.competition_name or "")
                       or "高教社杯" in (c.competition_name or "")]
                if _cn:
                    selected = max(_cn, key=lambda c: c.document_year)
                    return _implicit_version_result(selected, _cn)
            selected = max(_model_pool, key=lambda c: c.document_year)
            return _implicit_version_result(selected, _model_pool)
    # 智能车类问法显式纠正：「智能车」不是「智能汽车」子串，易被匹配丢弃而锁不住赛事，
    # 导致「智能车怎么备赛」走兜底、答非所问。
    if "智能车" in q or "智能汽车" in q:
        _car_pool = [c for c in comps
                     if "智能汽车" in (c.competition_name or "") or "智能车" in (c.competition_name or "")]
        if _car_pool:
            selected = max(_car_pool, key=lambda c: c.document_year)
            return _implicit_version_result(selected, _car_pool)
    mentioned_years = {int(y) for y in re.findall(r"20\d{2}", q)}
    scored = [(c, _competition_match_score(q_match, c)) for c in comps]
    scored = [(c, score) for c, score in scored if score >= 4]
    if not scored:
        return None, None, None

    if mentioned_years:
        year_matches = [(c, score) for c, score in scored if c.document_year in mentioned_years]
        if year_matches:
            best_score = max(score for _, score in year_matches)
            matches = [item for item, score in year_matches if score == best_score]
            return _implicit_version_result(matches[0], matches)
        return None, None, "库中没有你指定年份的该赛事通知，请确认年份或查看官网最新公告。"

    best = max(scored, key=lambda item: item[1])[0]
    best_score = max(score for _, score in scored)
    if len({_norm(c.competition_name) for c, score in scored if score == best_score}) > 1:
        return None, None, "问题可能指向多个赛事，请补充完整赛事名称或赛事 ID。"
    same_name = [c for c, _ in scored if _norm(c.competition_name) == _norm(best.competition_name)]
    if len(same_name) == 1:
        return best, None, None

    verified = [c for c in same_name if assess_source_readiness(c).ready]
    years = "、".join(str(c.document_year) for c in sorted(same_name, key=lambda item: item.document_year, reverse=True))
    if verified:
        selected = max(verified, key=lambda item: item.document_year)
        note = f"你没有指定年份，以下使用最新官网来源已确认版本：{selected.document_year}年（{selected.doc_version}）。"
        return selected, note, None
    clarification = f"“{best.competition_name}”存在多个年份版本（{years}），且暂无官网来源与关键证据完整的版本。请明确年份后再查询。"
    return None, None, clarification


def _resolve_competition(question: str, comps: list[Competition]) -> Optional[Competition]:
    """从问题里锁定最匹配的赛事；无明显匹配返回 None。

    打分优先级：完整名子串命中 > 核心名子串命中 > 赛事ID子串命中，
    再叠加年份加权，避免「互联网+」被「蓝桥杯」的 LCS 噪声抢走。
    """
    return _resolve_competition_versioned(question, comps)[0]


# ---------------------------------------------------------------------------
# 工具：意图判定（规则）
# ---------------------------------------------------------------------------

_KW_RECOMMEND = ["推荐", "适合我", "适合参加", "该参加", "选哪个", "报哪个", "有什么比赛", "参加什么"]
_KW_TEAMMATE = [
    "推荐队友", "匹配队友", "找队友", "找搭档", "组队搭档", "互补队友",
    "队友推荐", "组队成员", "想找队友", "帮我找队友", "缺队友", "需要队友", "组队缺",
]
_KW_TEAM = ["组队招募", "招募", "招新", "招人", "招队友", "文案", "写一份", "招队员", "找人组队"]
_KW_DETAIL = ["介绍", "详情", "是什么", "概况", "简介", "了解一下"]
# 联网补充触发词：用户明显想要「最新 / 官方之外」的动态时，主动补联网检索
_WEB_TRIGGER = [
    "最新", "2026", "2025", "2024", "新闻", "官网", "今年", "最近", "动态",
    "报名开始", "通知", "公告", "刚出", "新增", "更新", "实时", "网上", "查一下",
    # 开放/建议类问题也主动联网，补充真实「备赛攻略 / 经验 / 含金量评价」
    "备赛", "备考", "备战", "攻略", "经验", "怎么准备", "如何准备", "怎么学",
    "含金量", "值得参加", "难不难", "前景", "评价", "靠谱吗", "避坑", "注意什么",
    "复习", "冲刺", "怎么规划", "如何规划", "经验分享",
]
_KW_CHAT = [
    "规划", "计划", "建议", "怎么准备", "如何准备", "怎么选", "如何选",
    "策略", "路线", "方向", "经验", "分享", "攻略", "提升", "怎么学",
    "如何学", "怎么备", "如何备", "我该怎么", "该不该", "值不值", "怎么样",
    "含金量", "值得参加", "值得报", "难不难", "难吗", "有没有用", "有用吗",
    "前景", "优势", "评价", "靠谱吗", "好不好", "如何准备", "怎么冲刺",
    "如何冲", "怎么冲", "怎么安排", "如何安排", "注意什么", "避坑",
    # —— 以下扩充：覆盖「备赛 / 备考 / 备战」整类问法，避免被误判为规则问答 ——
    "备赛", "备考", "备战", "怎么备考", "如何备考", "怎么打", "如何打",
    "复习", "冲刺", "学习路线", "时间规划", "怎么规划", "如何规划",
    "备考攻略", "经验分享", "备赛经验", "经验贴", "怎么提升", "如何提升",
    "提升路径", "能力提升", "怎么练", "如何练", "该如何准备", "从零开始",
    # 指定了具体赛事时的「选赛项 / 是否适合我」也走 chat 给针对性建议，而非全局推荐
    "选哪个", "选什么", "报哪个", "报什么", "适合我", "适合吗", "适合打", "适合报",
]


def _classify_intent(question: str, has_comp: bool) -> str:
    q = question
    if any(k in q for k in _KW_TEAMMATE):
        return "teammate"
    if any(k in q for k in _KW_TEAM):
        return "team"
    if has_comp and "介绍" not in q and (_asks_registration_deadline(q) or _asks_team_size(q)):
        return "qa"
    if has_comp and any(k in q for k in _KW_DETAIL):
        return "detail"
    # 开放/建议/评价/选型类问题（怎么准备、值不值得、含金量、选赛项、是否适合我…）
    # 优先走 chat：由 LLM 或确定性生成器基于赛事证据+通用方法论作答。即便 LLM 未启用，
    # chat 也有基于证据的兜底回答（备赛路线/价值研判/选型建议），不再被当成 qa 拒答、
    # 也不会因「未通过推荐门控」而答非所问。
    if has_comp and any(k in q for k in _KW_CHAT):
        return "chat"
    if any(k in q for k in _KW_RECOMMEND):
        return "recommend"
    if has_comp:
        return "qa"          # 指向具体赛事的事实查询，走规则问答（检索带引用）
    # 无明确赛事：开放/建议/通用类问题走自由对话（chat），由 LLM 当参谋
    return "chat"


# ---------------------------------------------------------------------------
# 工具：Citation 序列化 / 引用编号
# ---------------------------------------------------------------------------


def _cite_dict(c: Citation) -> dict:
    return c.model_dump(mode="json")


def _cite_marker(idx: int) -> str:
    return f"[{idx + 1}]"


def _asks_registration_deadline(question: str) -> bool:
    """识别报名截止问法，交给结构化日期做唯一事实源。"""
    text = (question or "").strip().lower()
    asks_registration = "报名" in text
    asks_time = any(word in text for word in ("截止", "日期", "时间", "什么时候", "哪天"))
    return asks_registration and asks_time


def _asks_team_size(question: str) -> bool:
    """识别团队人数问法，优先返回对应结构化字段及页级证据。"""
    text = (question or "").strip().lower()
    asks_team = any(word in text for word in ("团队", "队伍", "组队", "队员", "一队"))
    asks_size = any(word in text for word in ("几人", "几个人", "人数", "多少人", "规模"))
    return asks_team and asks_size


def is_followup_question(question: str) -> bool:
    text = question.strip()
    return bool(re.search(r"这个比赛|该赛事|这个赛事|上面.*比赛|它的|它还能", text)) or text in (
        "报名还来得及吗？", "报名还来得及吗", "还能报名吗？", "还能报名吗",
        "可以跨校和跨专业组队吗？", "几个人一队？",
    )


def _requested_fields(question: str) -> set[str]:
    fields = set()
    if _asks_registration_deadline(question):
        fields.add("registration_deadline")
    if _asks_team_size(question) or any(w in question for w in ("个人", "组队", "跨校", "专业")):
        fields.update(("team_min", "team_max"))
    if any(w in question for w in ("材料", "交哪些", "交什么", "哪些东西")):
        fields.add("required_materials")
    if any(w in question for w in ("专业", "学历", "毕业", "在读", "报名条件", "能报", "参加吗", "来得及")):
        fields.update(("eligible_students", "allowed_majors", "allowed_grades"))
    if any(w in question for w in ("比赛结束", "初赛结束", "竞赛结束")):
        fields.add("competition_end_date")
    if any(w in question for w in ("多少钱", "费用", "收费", "报名费", "参赛费")):
        fields.add("registration_fee")
    return fields


# 仅含符号/空白、无任何可理解语义的输入（如纯标点、纯表情、乱敲的字符）。
# 用于入口前置拦截，避免对垃圾输入套用画像给出无意义闲聊。
_MEANINGFUL_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf0-9a-zA-Z]"  # 至少一个汉字 / 数字 / 英文字母
)


def _is_meaningful_question(question: str) -> bool:
    """输入是否含可理解语义（至少一个汉字/数字/字母）。纯符号噪声返回 False。"""
    q = (question or "").strip()
    if len(q) < 2:
        return False
    return bool(_MEANINGFUL_RE.search(q))


# ---------------------------------------------------------------------------
# 工具：可选 LLM 润色（默认关闭，不影响引用与门控结论）
# ---------------------------------------------------------------------------


def _llm_polish(draft: str, context: str, model: Optional[str] = None) -> str:
    """若启用 LLM，则对确定性草稿做自然语言润色；否则原样返回。

    仅润色措辞，绝不改写引用编号、门控结论与数值——这些来自本地可信数据。
    实现见 src/agent/llm.py（OpenAI 兼容，含引用安全校验与优雅回退）。
    ``model`` 可选，请求级覆盖默认模型（前端模型选择器透传）。
    """
    from agent.llm import polish as _polish

    out = _polish(draft, context, model=model)
    return out if out else draft


# ---------------------------------------------------------------------------
# 节点 1：意图路由
# ---------------------------------------------------------------------------


def node_route_intent(state: AgentState) -> AgentState:
    question = (state.get("question") or "").strip()
    trace = list(state.get("trace") or [])

    comps = db.get_all_competitions()

    # 优先使用前端显式指定的赛事
    resolved: Optional[Competition] = None
    version_note: Optional[str] = None
    clarification: Optional[str] = None
    if state.get("competition_id"):
        resolved = db.get_competition(state["competition_id"])
    if resolved is None:
        resolved, version_note, clarification = _resolve_competition_versioned(question, comps)
    if resolved is None and not clarification and is_followup_question(question):
        context_id = state.get("context_competition_id")
        resolved = db.get_competition(context_id) if context_id else None
        if resolved:
            version_note = f"沿用当前对话赛事：{resolved.competition_name}（{resolved.document_year}）。"
    if resolved is None and not clarification and re.search(r"[\u4e00-\u9fffA-Za-z]+(?:杯|大赛|竞赛)", question):
        clarification = "未能确认你提到的赛事名称或版本，请提供完整名称、年份或官方链接；不能用另一赛事的规则代答。"

    intent = _classify_intent(question, has_comp=resolved is not None)
    trace.append(
        f"意图路由：intent={intent}"
        + (f"，锁定赛事={resolved.competition_name}" if resolved else "，未锁定具体赛事")
    )

    return {
        **state,
        "intent": intent,
        "resolved_competition": resolved.competition_id if resolved else None,
        "resolved_name": resolved.competition_name if resolved else None,
        "version_resolution_note": version_note,
        "clarification": clarification,
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# 节点 2：赛事隔离检索
# ---------------------------------------------------------------------------


def _detail_fallback_citations(comp: Optional[Competition]) -> list[Citation]:
    """Return existing field evidence without disguising generated summaries as quotes."""
    if comp is None:
        return []
    fields = {"team_min", "team_max", "eligible_students", "registration_deadline",
              "registration_deadline_at", "submission_deadline", "submission_deadline_at",
              "required_materials", "required_skills"}
    return [item.model_copy(deep=True) for item in comp.evidence if item.field in fields and item.source_text.strip()
            and item.anchor_status not in ("invalid", "needs_review")]


def _broad_retrieve(question: str, top_k: int = 5) -> tuple[list, list]:
    """开放对话（chat）用：跨全部赛事做轻量召回，作为 LLM 自由作答的接地证据。

    不依赖 RAG 语义检索（本环境 RAG 为本地隔离实现），改用「赛事名模糊匹配 +
    关键字段（技能/对象/类别）关键词重叠」打分，取 top-k 赛事，再把每个赛事的
    已保存的原文作为证据。返回 (相关赛事列表, 证据列表)。
    """
    comps = db.get_all_competitions()
    scored = []
    q = _alias_expand(question)
    for c in comps:
        score = _competition_match_score(q, c)
        hay_parts = [c.competition_name or ""]
        hay_parts += list(getattr(c, "required_skills", []) or [])
        hay_parts += [getattr(s, "value", str(s)) for s in (getattr(c, "eligible_students", []) or [])]
        cat = getattr(c, "category", None)
        if cat:
            hay_parts.append(str(cat))
        hay = " ".join(hay_parts)
        if any(tok in q for tok in hay.split() if len(tok) >= 2):
            score += 2
        if score >= 4:
            scored.append((c, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in scored[:top_k]]
    cites: list = []
    for c in top:
        for cit in _detail_fallback_citations(c)[:3]:
            cites.append(cit)
    return top, cites


def node_retrieve(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    if state.get("clarification"):
        trace.append("隔离检索：跳过（赛事版本尚未明确）")
        return {**state, "citations": [], "trace": trace}
    cid = state.get("resolved_competition")
    if not cid:
        if (state.get("intent") or "") == "chat":
            top, cites = _broad_retrieve(state.get("question") or "", top_k=int(state.get("top_k") or 5))
            trace.append(f"开放检索：跨赛事召回 {len(top)} 个相关赛事、{len(cites)} 条证据")
            return {**state, "citations": [_cite_dict(h) for h in cites], "trace": trace}
        trace.append("隔离检索：跳过（无目标赛事）")
        return {**state, "citations": [], "trace": trace}

    comp = db.get_competition(cid)
    top_k = int(state.get("top_k") or 4)
    hits = get_rag().query(
        cid,
        state.get("question") or "",
        document_year=comp.document_year if comp else None,
        top_k=top_k,
    )
    # 截止日期只能由结构化 Ground Truth 给出；RAG 仅返回同字段的一条页级证据。
    # 这样既保留可追溯原文，又不会把报名开始日、校赛日或提交日误写成第二个截止日。
    requested = _requested_fields(state.get("question") or "")
    if comp is not None and _asks_registration_deadline(state.get("question") or ""):
        deadline_evidence = [e for e in comp.evidence if e.field == "registration_deadline"
                             and e.source_text.strip() and e.anchor_status not in ("invalid", "needs_review")]
        if deadline_evidence:
            hits = deadline_evidence[:1] + [e for e in comp.evidence
                if e.field in requested - {"registration_deadline"} and e.source_text.strip()
                and e.anchor_status not in ("invalid", "needs_review")]
        else:
            hits = []
        trace.append("隔离检索：报名截止问题仅保留 registration_deadline 单一证据")
    if (
        comp is not None
        and state.get("intent") == "qa"
        and _asks_team_size(state.get("question") or "")
    ):
        team_evidence = [e for e in comp.evidence if e.field in {"team_min", "team_max"}
                         and e.source_text.strip() and e.anchor_status not in ("invalid", "needs_review")]
        if team_evidence:
            hits = team_evidence[:top_k]
        else:
            hits = []
        trace.append("隔离检索：团队人数问题仅保留 team_min/team_max 对应证据")
    # detail 介绍类：保证「介绍一下X」始终带完整官方结构化引用。
    # 语义/关键词检索为空 → 整段兜底；检索有结果 → 在其基础上补全缺失关键字段。
    if state.get("intent") == "detail" and not _asks_registration_deadline(state.get("question") or ""):
        if not hits:
            hits = _detail_fallback_citations(comp)
            trace.append("隔离检索：detail 兜底返回关键字段证据")
        else:
            existing = {getattr(h, "field", None) for h in hits}
            extra = [c for c in _detail_fallback_citations(comp) if c.field not in existing]
            if extra:
                hits = list(hits) + extra
                trace.append(f"隔离检索：detail 补全 {len(extra)} 条关键字段证据")
    # qa 事实查询：语义检索为空时用赛事结构化关键字段兜底，避免「未检索到相关
    # 条款」式拒答——评价/开放类问题（已走 chat）更应如此；即便纯事实问法检索
    # 落空，也至少能基于团队/对象/截止/材料/技能给出可溯源答复。
    if (state.get("intent") == "qa" and not hits and comp is not None
            and not _asks_registration_deadline(state.get("question") or "")
            and not _asks_team_size(state.get("question") or "")):
        hits = _detail_fallback_citations(comp)
        if hits:
            trace.append("隔离检索：qa 兜底返回关键字段证据")
    # chat 开放/建议类（已锁定具体赛事）：RAG 命中为空时，用赛事结构化关键字段
    # 兜底，保证「文科生能玩吗 / 怎么备赛」这类开放问法也带官方引用（团队/对象/
    # 技能/截止），而非回退成无出处的纯观点。与 detail/qa 兜底逻辑一致。
    if state.get("intent") == "chat" and not hits and comp is not None:
        hits = _detail_fallback_citations(comp)
        if hits:
            trace.append("隔离检索：chat 兜底返回关键字段证据")
    if comp is not None and state.get("intent") == "qa" and requested:
        hits = [e for e in comp.evidence if e.field in requested and e.source_text.strip()
                and e.anchor_status not in ("invalid", "needs_review")]
        # Fees have no structured field in older data; only fee-specific official quotes qualify.
        if "registration_fee" in requested:
            hits += [e for e in comp.evidence if e not in hits and e.source_text.strip()
                     and re.search(r"(?:参赛费|报名费|收费|费用|免费)", e.source_text)
                     and e.anchor_status not in ("invalid", "needs_review")]
    trace.append(f"隔离检索：collection=rag_{cid}，命中 {len(hits)} 条证据")
    return {
        **state,
        "citations": [_cite_dict(h) for h in hits],
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# 节点 2.5：联网补充（可选，仅作本地官方证据的补充，不混入 [n] 引用）
# ---------------------------------------------------------------------------


def node_web_augment(state: AgentState) -> AgentState:
    """对 qa/detail/chat 意图，在本地证据偏薄或用户明显要「最新动态」时，
    调用可选联网搜索作为补充。结果存于 ``web_results``，由 compose 明确标注，
    绝不与已核验的官方 [n] 引用混在一起。未启用 / 失败均返回空，不影响主链路。
    """
    trace = list(state.get("trace") or [])
    if state.get("clarification") or not _web_enabled():
        return {**state, "web_results": []}
    intent = state.get("intent") or ""
    if intent not in ("qa", "detail", "chat"):
        return {**state, "web_results": []}
    q = state.get("question") or ""
    cites = state.get("citations") or []
    wants_web = any(k in q for k in _WEB_TRIGGER)
    # 仅当「显式要联网」或「本地证据偏薄（<3 条）」时才补充，避免每次都打外部 API
    if not wants_web and len(cites) >= 3:
        return {**state, "web_results": []}
    results = _web_search(q) or []
    if results:
        trace.append(f"联网补充：检索到 {len(results)} 条网络结果（标注为参考，不混入官方引用）")
    else:
        trace.append("联网补充：未启用或检索无结果，保持纯本地作答")
    return {**state, "web_results": results, "trace": trace}


# ---------------------------------------------------------------------------
# 节点 3：硬性门控
# ---------------------------------------------------------------------------


def _load_profile(state: AgentState) -> Optional[UserProfile]:
    uid = state.get("user_id")
    if not uid:
        return None
    profile = db.get_user_profile(uid)
    if profile is None:
        return None
    question = state.get("question") or ""
    updates = {}
    numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "七": 7, "八": 8, "九": 9, "十": 10}
    match = re.search(r"(?:我们|队伍|团队|改成|现在是|我)(?:现在|已经|有|是|改成|共|一共|总共)*\s*(\d+|[一二两三四五六七八九十])\s*(?:个)?人", question)
    if match:
        raw = match.group(1)
        size = int(raw) if raw.isdigit() else numbers[raw]
        if size > 0:
            updates["expected_team_size"] = size
    if re.search(r"(?:我|我们).*?(?:已经)?毕业|不(?:再|是)在(?:校|读)", question):
        updates.update(education_level="毕业生（毕业5年内）", grade="已毕业")
    # Current-turn statements affect this answer only; never mutate the saved profile.
    return profile.model_copy(update=updates) if updates else profile


def node_gate(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    cid = state.get("resolved_competition")
    profile = _load_profile(state)
    comp = db.get_competition(cid) if cid else None
    pending_review = bool(comp and not assess_source_readiness(comp).ready)
    if comp is None or profile is None:
        trace.append("硬性门控：跳过（缺用户画像或赛事）")
        return {**state, "gate": {"skipped": True}, "pending_review": pending_review, "trace": trace}

    today = contest_now()
    problems = data_validity_check(comp, today)
    if problems:
        # Candidate information remains readable, without an eligibility verdict.
        gate = {
            "eligible": False,
            "source_issues": [f"来源待核实：{p}" for p in problems],
            "reasons": [],
            "actions": [],
        }
        trace.append("硬性门控：官方来源尚待核验（不阻断信息查阅）")
        return {**state, "gate": gate, "pending_review": not assess_source_readiness(comp).ready, "trace": trace}

    eligible, reasons = eligibility_gate(profile, comp, today)
    gate = {
        "eligible": eligible,
        "source_issues": [],
        "reasons": [r.reason for r in reasons],
        "actions": [r.possible_action for r in reasons if r.possible_action],
    }
    trace.append(f"硬性门控：{'通过' if eligible else '一票否决'}（{len(reasons)} 条原因）")
    return {
        **state,
        "gate": gate,
        "pending_review": not assess_source_readiness(comp).ready,
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# 节点 4：软性评分
# ---------------------------------------------------------------------------


def node_score(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    cid = state.get("resolved_competition")
    profile = _load_profile(state)
    gate = state.get("gate") or {}

    if not cid or profile is None:
        trace.append("软性评分：跳过（缺用户画像或赛事）")
        return {**state, "score": {"skipped": True}, "trace": trace}
    if gate.get("eligible") is not True:
        trace.append("软性评分：跳过（门控未通过，不进入评分）")
        return {**state, "score": {"skipped": True, "reason": "门控未通过"}, "trace": trace}

    comp = db.get_competition(cid)
    bd = soft_match_score(profile, comp, contest_now())
    trace.append(f"软性评分：总分 {bd.total}（S{bd.skill_score}/E{bd.experience_score}/R{bd.resource_score}/W{bd.workload_score}）")
    return {**state, "score": bd.model_dump(mode="json"), "trace": trace}


# ---------------------------------------------------------------------------
# 节点 5：组队招募文案
# ---------------------------------------------------------------------------


def node_team_copy(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    cid = state.get("resolved_competition")
    if not cid:
        trace.append("组队文案：跳过（无目标赛事）")
        return {**state, "team_copy": "", "trace": trace}

    comp = db.get_competition(cid)
    profile = _load_profile(state)
    citations = state.get("citations") or []
    source_ready = assess_source_readiness(comp).ready
    if not source_ready:
        trace.append("组队文案：跳过（关键证据待补充）")
        copy = "该赛事目前仅作为候选信息展示，关键字段证据尚不完整，暂不生成组队招募结论。请先查看赛事官网最新通知。"
        return {**state, "team_copy": copy, "pending_review": True, "trace": trace}

    readiness = assess_recommendation_readiness(comp, contest_now())
    if not readiness.ready:
        trace.append("组队文案：跳过（当前不可报名）")
        copy = "该赛事当前不处于可报名状态，暂不生成新的组队招募文案。请查看官网最新通知；历史材料仍可在赛事目录中查看。"
        return {**state, "team_copy": copy, "trace": trace}
    gate = state.get("gate") or {}
    if gate.get("eligible") is not True:
        reasons = "；".join(gate.get("reasons") or []) or "尚缺少可确认个人资格的画像"
        copy = f"你暂不符合该赛事硬性条件或资格尚未确认：{reasons}。暂不生成可参赛招募，请先核对身份、人数与官网规则。"
        trace.append("组队文案：跳过（个人资格未通过）")
        return {**state, "team_copy": copy, "trace": trace}

    team_txt = format_team_size(comp.team_min, comp.team_max)
    skills = "、".join(comp.required_skills) if comp.required_skills else "相关技能不限"
    deadline = format_deadline(comp)
    my_skills = "、".join(profile.skills) if profile and profile.skills else "（可在个人画像补充）"

    # 引用编号：把 team/skill 相关证据映射到 [n]
    def _mark_for(field_key: str) -> str:
        for i, c in enumerate(citations):
            if c.get("field") in field_key:
                return _cite_marker(i)
        return ""

    team_mark = _mark_for(("team_max", "team_min"))
    skill_mark = _mark_for(("required_skills",))
    reg_mark = _mark_for(("registration_deadline",))

    copy = (
        f"【{comp.competition_name}（{comp.document_year}）· 组队招募】\n\n"
        f"🚀 我们正在为「{comp.competition_name}」组队，诚招志同道合的队友！\n\n"
        f"· 队伍规模：{team_txt}{team_mark}\n"
        f"· 需要能力：{skills}{skill_mark}\n"
        f"· 报名截止：{deadline}{reg_mark}\n"
        f"· 我能带来：{my_skills}\n\n"
        f"如果你对{('、'.join(comp.evaluation_dimensions) if comp.evaluation_dimensions else '这项赛事')}感兴趣，"
        f"欢迎私信我一起冲！报名信息以官方通知为准。"
    )
    copy = _llm_polish(copy, f"赛事={comp.competition_name}，队伍={team_txt}，技能={skills}，截止={deadline}", model=state.get("model"))
    trace.append("组队文案：已生成（含引用编号）")
    return {**state, "team_copy": copy, "pending_review": False, "trace": trace}


# ---------------------------------------------------------------------------
# 节点 5.5：基于画像的互补队友推荐（teammate intent 专用）
# ---------------------------------------------------------------------------
def node_teammate_match(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    seeker = _load_profile(state)
    if seeker is None:
        trace.append("队友推荐：跳过（无可用画像）")
        return {**state, "teammate_matches": [], "trace": trace}

    candidates = db.list_demo_profiles(exclude_user_id=seeker.user_id)
    matches = recommend_teammates(seeker, candidates, top_k=state.get("top_k") or 5)
    matches_dict = [m.model_dump(mode="json") for m in matches]
    trace.append(f"队友推荐：从 {len(candidates)} 名候选中匹配出 {len(matches)} 名互补队友")
    return {**state, "teammate_matches": matches_dict, "trace": trace}


# ---------------------------------------------------------------------------
# 节点 6：全库推荐（recommend intent 专用）
# ---------------------------------------------------------------------------


def node_recommend_all(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    profile = _load_profile(state)
    if profile is None:
        trace.append("全库推荐：跳过（无用户画像）")
        return {**state, "recommendations": [], "trace": trace}

    comps = db.get_all_competitions()
    results = recommend_for_user(profile, comps, contest_now())
    formal = [r for r in results if r.eligible and r.score is not None][:5]
    selected = formal

    # 为每条可推荐赛事附 1 条最有代表性的引用（团队/技能）
    rag = get_rag()
    recos: list[dict] = []
    all_citations: list[dict] = list(state.get("citations") or [])
    for r in selected:
        comp = db.get_competition(r.competition_id)
        cite_idx = None
        try:
            hits = rag.query(r.competition_id, "参赛对象与团队人数要求",
                             document_year=comp.document_year if comp else None, top_k=1)
            if hits:
                all_citations.append(_cite_dict(hits[0]))
                cite_idx = len(all_citations) - 1
        except Exception:
            pass
        recos.append({
            "competition_id": r.competition_id,
            "competition_name": r.competition_name,
            "recommendation_status": r.recommendation_status,
            "score": r.score,
            "urgent": r.urgent,
            "pending_review": r.pending_review,
            "cite_index": cite_idx,
        })

    trace.append(
        f"全库推荐：共检查 {len(results)} 条，正式推荐 {len(formal)} 条；候选信息仅在赛事目录展示"
    )
    return {**state, "recommendations": recos, "citations": all_citations, "trace": trace}


# ---------------------------------------------------------------------------
# 节点 7：组装答案（自动带引用）
# ---------------------------------------------------------------------------


def _citations_appendix(citations: list[dict]) -> str:
    if not citations:
        return ""
    lines = ["\n\n——— 引用来源 ———"]
    for i, c in enumerate(citations):
        page = c.get("page")
        loc = f"第{page}页" if page not in (None, -1) else "未定位页码"
        doc = c.get("document_name") or "官方通知"
        lvl = c.get("trusted_level") or "C"
        txt = (c.get("source_text") or "").strip().replace("\n", " ")
        if len(txt) > 60:
            txt = txt[:60] + "…"
        lines.append(f"{_cite_marker(i)} {doc}·{loc}·{lvl}级：{txt}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具：混合模式 —— LLM 基于证据自由撰写（仍强约束 [n] 引用）
# ---------------------------------------------------------------------------

_CIT_RE = re.compile(r"\[(\d+)\]")


def _build_evidence_brief(citations: list[dict], web_results: Optional[list] = None) -> str:
    """把 citations 序列化成 LLM 能读懂的「编号证据清单」。

    每条形如：[n] （文档名｜字段｜可信等级）原文片段 官方链接
    LLM 撰写时须用 [n] 回指，从而保证所有事实可溯源。
    ``web_results`` 作为单独「🌐 联网参考」块附在末尾，明确标注仅供参考、
    不计入 [n] 官方引用，避免与已核验来源混淆。
    """
    if not citations and not web_results:
        return "（无具体赛事证据，请基于通用方法论回答，不要编造任何具体赛事的事实、日期或数字）"
    lines = []
    for i, c in enumerate(citations):
        doc = c.get("document_name") or "官方通知"
        url = c.get("source_url") or ""
        field = c.get("field") or ""
        txt = (c.get("source_text") or "").strip().replace("\n", " ")
        lvl = c.get("trusted_level") or "C"
        line = f"[{i + 1}] （{doc}｜字段:{field}｜可信等级:{lvl}）{txt}"
        if url:
            line += f" 官方链接:{url}"
        lines.append(line)
    if web_results:
        lines.append("\n🌐 联网参考（仅供参考，不得用于 [n] 引用，也不计入官方证据）：")
        for i, w in enumerate(web_results[:4]):
            title = " ".join((w.get("title") or "").split())
            url = w.get("url") or ""
            snippet = " ".join((w.get("snippet") or "").split())
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            lines.append(f"  - {title}：{snippet} {url}")
    return "\n".join(lines)


def _profile_summary(profile) -> str:
    if not profile:
        return ""
    parts = []
    edu = getattr(profile, "education_level", None)
    if edu:
        parts.append(f"学历：{edu}")
    if getattr(profile, "major", None):
        parts.append(f"专业：{profile.major}")
    if getattr(profile, "skills", None):
        parts.append("技能：" + "、".join(profile.skills))
    if getattr(profile, "interests", None):
        parts.append("兴趣：" + "、".join(profile.interests))
    return "；".join(parts)


def _sanitize_citations(answer: str, n_citations: int) -> str:
    """去掉超出证据范围的 [n] 引用，防止模型编造不存在的编号。

    若模型写了 [7] 但只有 3 条证据，则该标记被剥离，避免前端渲染幽灵引用。
    """
    def _repl(m: "re.Match") -> str:
        idx = int(m.group(1))
        return "" if idx < 1 or idx > n_citations else m.group(0)
    return _CIT_RE.sub(_repl, answer)


# ---------------------------------------------------------------------------
# 工具：开放/建议类问题的「确定性作答」（LLM 未启用时也能真正回答问题）
# ---------------------------------------------------------------------------

# 编程 / 算法类赛事识别（用于给出针对性的备赛路线）
_PROG_KW = (
    "算法", "程序设计", "编程", "C/C++", "C++", "Java", "Python", "软件开发",
    "Web应用", "网络安全", "软件赛", "计算机", "数据结构",
)

# 数学建模类赛事识别（category=modeling/math 或名称/技能含建模关键词）
_MODELING_KW = (
    "数学建模", "建模", "MathorCup", "APMCM", "MCM", "ICM", "电工杯", "深圳杯",
    "统计建模", "五一数学", "华为杯数学", "华中杯", "华东杯", "中青杯",
)
_INNOV_KW = (
    "创新创业", "创业计划", "创新大赛", "商业计划", "路演", "红旅", "互联网+",
    "互联网＋", "三创赛", "学创杯", "微创业", "挑战杯", "大创",
)


def _is_math_modeling_comp(comp) -> bool:
    if comp is None:
        return False
    cat = str(getattr(comp, "category", ""))
    if cat in ("modeling", "math"):
        return True
    # 工程/硬件/设计类的「建模」指产品/结构建模，非数学建模，排除避免误判
    if cat in ("engineering", "electronics", "design", "physics"):
        return False
    blob = " ".join(
        [str(getattr(comp, "competition_name", ""))]
        + list(getattr(comp, "required_skills", []) or [])
        + list(getattr(comp, "evaluation_dimensions", []) or [])
    )
    return any(k in blob for k in _MODELING_KW)


def _is_innovation_comp(comp) -> bool:
    if comp is None:
        return False
    cat = str(getattr(comp, "category", ""))
    if cat == "innovation":
        return True
    blob = " ".join(
        [str(getattr(comp, "competition_name", ""))]
        + list(getattr(comp, "required_skills", []) or [])
        + list(getattr(comp, "evaluation_dimensions", []) or [])
    )
    return any(k in blob for k in _INNOV_KW)


_MECH_KW = (
    "机械", "结构", "车辆", "智能汽车", "成图", "工程训练", "工程创新", "产品信息建模",
)
_PHYS_KW = ("物理", "CUPT", "物理实验")
_ENG_KW = ("英语", "外语", "演讲", "写作", "翻译", "口语", "辩论", "NECCS")

# 明确的软件/算法类类别优先判真；其余学科/硬件/语言/商科类即使 skills 含「算法」也不归编程模板
_HW_ACAD_CATS = (
    "electronics", "engineering", "physics", "english", "chem_env", "life_science",
    "math", "modeling", "design", "business", "data", "logistics", "robotics_ai",
    "innovation",
)


def _is_electronics_comp(comp) -> bool:
    if comp is None:
        return False
    if str(getattr(comp, "category", "")) == "electronics":
        return True
    return "电子设计" in (comp.competition_name or "")


def _is_engineering_comp(comp) -> bool:
    if comp is None:
        return False
    cat = str(getattr(comp, "category", ""))
    # 机械/工程 + 智能车/机器人（控制+嵌入式，属硬件工程）走工程模板
    if cat in ("engineering", "robotics_ai"):
        return True
    if cat in ("software", "programming", "data", "math", "modeling", "english", "business"):
        return False
    blob = " ".join(
        [comp.competition_name or ""]
        + list(getattr(comp, "required_skills", []) or [])
    )
    return any(k in blob for k in _MECH_KW)


def _is_physics_comp(comp) -> bool:
    if comp is None:
        return False
    if str(getattr(comp, "category", "")) == "physics":
        return True
    return "物理" in (comp.competition_name or "")


def _is_english_comp(comp) -> bool:
    if comp is None:
        return False
    if str(getattr(comp, "category", "")) == "english":
        return True
    return any(k in (comp.competition_name or "") for k in _ENG_KW)


def _is_programming_comp(comp) -> bool:
    if comp is None:
        return False
    cat = str(getattr(comp, "category", ""))
    if cat in ("software", "programming"):
        return True
    if cat in _HW_ACAD_CATS:
        return False
    blob = " ".join(
        [cat]
        + list(getattr(comp, "required_skills", []) or [])
        + list(getattr(comp, "evaluation_dimensions", []) or [])
    )
    return any(k in blob for k in _PROG_KW)


def _is_prep_question(q: str) -> bool:
    return any(k in q for k in (
        "备赛", "备考", "备战", "怎么准备", "如何准备", "怎么备", "如何备",
        "怎么备考", "如何备考", "怎么打", "如何打", "复习", "冲刺", "攻略",
        "学习路线", "时间规划", "怎么规划", "如何规划", "备考攻略", "备赛经验",
        "经验贴", "怎么提升", "如何提升", "提升路径", "能力提升", "怎么练",
        "如何练", "该如何准备", "从零开始", "怎么学", "如何学",
    ))


def _is_eval_question(q: str) -> bool:
    return any(k in q for k in (
        "含金量", "值得参加", "值得报", "难不难", "难吗", "有没有用", "有用吗",
        "前景", "优势", "评价", "靠谱吗", "好不好", "该不该", "值不值", "怎么样",
        "适合我", "适合吗", "适合打", "适合报",
    ))


def _is_selection_question(q: str) -> bool:
    return any(k in q for k in ("怎么选", "如何选", "选哪个", "选什么", "报哪个", "报什么"))


def _web_block(web_results, limit: int = 4) -> str:
    """把联网结果序列化为「🌐 参考」块（明确标注仅供参考、不混入官方引用）。"""
    if not web_results:
        return ""
    lines = ["\n🌐 联网补充参考（网友/媒体经验，仅供参考，请以官网为准）："]
    for w in web_results[:limit]:
        title = " ".join((w.get("title") or "").split())
        url = w.get("url") or ""
        snippet = " ".join((w.get("snippet") or "").split())
        if len(snippet) > 70:
            snippet = snippet[:70] + "…"
        lines.append(f"  · {title}：{snippet} {url}")
    return "\n".join(lines)


def _build_prep_guide(comp, web_results, profile) -> str:
    """生成可执行的备赛路线图（按赛事类别 + 用户画像定制）。"""
    name = comp.competition_name
    year = comp.document_year
    title = name if str(year) in str(name) else f"{name}（{year}）"
    lines = [f"关于「{title}」怎么备赛，我给你一份可直接落地的路线图（结合赛事结构 + 通用方法论）：\n"]

    facts = []
    if comp.registration_deadline:
        facts.append(f"报名/缴费截止 {format_deadline(comp)}")
    if comp.competition_start_date:
        facts.append(f"省赛/初赛时间约 {comp.competition_start_date.isoformat()}")
    if comp.eligible_students:
        facts.append("参赛对象：" + "、".join(comp.eligible_students))
    if comp.required_skills:
        facts.append("主要赛项/方向：" + "、".join(comp.required_skills))
    if facts:
        lines.append("📌 先锁定几个关键事实（来自官方通知）：")
        for f in facts:
            lines.append(f"  · {f}")
        lines.append("")

    if _is_electronics_comp(comp):
        branch = "electronics"
        lines.append(_build_electronics_prep(comp, profile))
    elif _is_engineering_comp(comp):
        branch = "engineering"
        lines.append(_build_engineering_prep(comp, profile))
    elif _is_physics_comp(comp):
        branch = "physics"
        lines.append(_build_physics_prep(comp, profile))
    elif _is_english_comp(comp):
        branch = "english"
        lines.append(_build_english_prep(comp, profile))
    elif _is_programming_comp(comp):
        branch = "programming"
        lines.append("🗺️ 软件/算法类备赛四阶段（建议提前 3–6 个月启动）：")
        lines.append("  1) 打基础（第1–6周）：吃透一门主力语言（C/C++ / Java / Python），"
                     "熟练掌握数据结构与基础算法——枚举、模拟、排序、贪心、递推递归、基础 DP、简单图论、哈希、字符串。")
        lines.append("  2) 专项突破（第7–12周）：按高频专题刷——数组/字符串、DFS/BFS、"
                     "动态规划、最短路、并查集、数论/组合数学、二分、前缀和/差分、单调栈/队列。")
        lines.append("  3) 真题实战（第13–18周）：限时刷近 5 年省赛真题，先稳拿填空/简单编程题，"
                     "再攻大题；每题复盘时间复杂度与更优解。")
        lines.append("  4) 冲刺模考（赛前2–4周）：全真 4 小时模拟，整理易错模板"
                     "（快读、二分、前缀和、并查集等），查漏补缺。")
        lines.append("")
        lines.append("📚 资料与平台：官网章程/样题/历年真题；洛谷、AcWing、蓝桥杯官方练习系统、"
                     "LeetCode 对应语言热题。")
        lines.append("💡 提分点：填空题重速度与准确性，编程大题重正确率与边界处理；"
                     "把标准库/STL 用熟能省大量赛场时间。")
    elif _is_math_modeling_comp(comp):
        branch = "modeling"
        lines.append(_build_math_modeling_prep(comp, profile))
    elif _is_innovation_comp(comp):
        branch = "innovation"
        lines.append(_build_innovation_prep(comp, profile))
    else:
        branch = "generic"
        lines.append("🗺️ 通用备赛四阶段（按赛制调整）：")
        lines.append("  1) 读懂赛制：研读竞赛章程、评分标准与往年获奖作品，明确「评什么、怎么评」。")
        lines.append("  2) 积累素材：按赛项补齐知识/技能短板；组队类尽早找互补队友、定选题方向。")
        lines.append("  3) 打磨作品/演练：做 1–2 轮完整模拟（含答辩/文档），按评分标准逐项自检。")
        lines.append("  4) 冲刺提交：预留时间做格式校对、查重/合规检查与最终提交，避免技术性失误。")
        lines.append("")
        lines.append("📚 资料与平台：竞赛官网与主办方通知、往年优秀作品集、指导老师与同好社群、相关公开课/教材。")

    if profile and branch in ("programming", "generic"):
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"优先用你最熟的语言或方向做主力，把短板专题（如算法/文档/答辩）排进前 6 周重点突破。"
            )

    web = _web_block(web_results)
    if web:
        lines.append(web)
    if comp.official_source_url:
        lines.append(f"\n🔗 官方入口：{comp.official_source_url}")
    return "\n".join(lines)


def _build_math_modeling_prep(comp, profile) -> str:
    """数学建模类备赛：建模流程 + 论文写作 + 常用算法/软件（按真实赛制差异化）。"""
    lines = []
    lines.append("🗺️ 数学建模备赛四阶段（建议提前 3–6 个月启动；团队通常 3 人分工：建模 / 编程求解 / 论文写作）：")
    lines.append("  1) 补基础（第1–6周）：一人主攻数学模型（评价/预测/优化/分类聚类方法），"
                 "一人主攻编程求解（MATLAB、Python 的 NumPy·SciPy·Pandas·Matplotlib），"
                 "一人主攻论文写作与 LaTeX 排版；三人都要懂建模全流程。")
    lines.append("  2) 方法专题（第7–12周）：刷透高频模型——评价类（AHP/模糊综合评价/TOPSIS）、"
                 "预测类（回归/时间序列/灰色预测/神经网络）、优化类（线性·整数·多目标规划，可用 Lingo）、"
                 "分类聚类（判别分析/K-means）、插值拟合、图论、微分方程、蒙特卡洛模拟。")
    lines.append("  3) 真题实战（第13–18周）：限时做近 3–5 年真题（高教社杯国赛、美赛 MCM/ICM、"
                 "华为杯、深圳杯、MathorCup 等），完整跑通「选题→假设→建模→求解→检验→写论文」全流程；每题复盘模型优劣。")
    lines.append("  4) 冲刺模考（赛前2–4周）：全真 3–4 天连续作战模拟（国赛 3 天、美赛 4 天），"
                 "重点练摘要写作（摘要定奖项档次）与时间分配，整理可复用代码模板。")
    lines.append("")
    lines.append("📝 论文结构（国赛/美赛 ICM 通用）：摘要（最关键，单独成页）→ 问题重述 → 模型假设 → "
                 "符号说明 → 模型建立与求解 → 模型检验/灵敏度分析 → 优缺点 → 参考文献 → 附录（代码/数据）。")
    lines.append("💡 提分点：摘要决定能否进国奖；假设要合理可解；模型重「思路清晰+结果可信」而非越复杂越好；"
                 "赛中学校通常提供机房，提前熟悉环境。")
    lines.append("📚 资料：司守奎《数学建模算法与应用》、清风数学建模、校苑数模；"
                 "真题来自全国大学生数学建模竞赛官网与各赛官网。")
    if profile:
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"数学底子好可主攻建模，编程熟负责求解，文笔好负责论文；把最弱的一环（建模思路/代码/写作）排进前 6 周重点补。"
            )
    return "\n".join(lines)


def _build_innovation_prep(comp, profile) -> str:
    """创新创业类备赛：商业计划书 BP + 路演 + 赛道选择（按真实赛制差异化）。"""
    lines = []
    lines.append("🗺️ 创新创业备赛四阶段（建议提前半年启动；团队 3–15 人，跨专业组队更稳）：")
    lines.append("  1) 定赛道与选题（第1–4周）：先选赛道——高教主赛道（创意组/初创组/成长组）、"
                 "青年红色筑梦之旅（红旅，偏乡村振兴/社会治理）、职教赛道、产业赛道（企业命题）、萌芽赛道（高中）。"
                 "选题抓真实痛点，尽量有落地数据与知识产权。")
    lines.append("  2) 做原型与验证（第5–12周）：做出 MVP/产品原型，跑通真实用户或试点，"
                 "攒商业数据（营收/用户/合同/专利/查新），把「创新性+商业性+社会价值」落到证据上。")
    lines.append("  3) 写 BP 与路演（第13–18周）：商业计划书按模块打磨——痛点与机会、解决方案与产品、"
                 "市场与竞品、商业模式、核心团队、财务预测与融资、里程碑与风险；路演 PPT 讲清「问题-方案-市场-模式-团队-数据」。")
    lines.append("  4) 冲刺答辩（赛前2–4周）：做 1–2 轮全真路演（校赛→省赛→国赛），"
                 "准备答辩高频问题（技术壁垒/数据来源/落地进展/知识产权/可持续性），按评审维度逐项自检。")
    lines.append("")
    lines.append("📊 评审维度：创新性、商业性（或社会价值/红旅）、团队能力、就业带动/落地可行。"
                 "金奖项目往往「真问题+真数据+真落地」，空想项目很难走远。")
    lines.append("💡 提分点：路演前 1 分钟电梯演讲最致命；BP 用数据说话、少堆形容词；"
                 "提前对接校创业学院/指导老师，用足学校孵化资源。")
    lines.append("📚 资料：大赛官网 cy.ncss.cn（原「互联网+」，现「中国国际大学生创新大赛」）、"
                 "往届金奖路演视频、校创新创业学院公开课。")
    if profile:
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"用你的专业/技能做项目核心技术或产品壁垒，再补商业与路演短板；跨专业组队（技术+商科+设计）最稳。"
            )
    return "\n".join(lines)


def _build_electronics_prep(comp, profile) -> str:
    """电子设计类备赛：硬件电路 + 嵌入式/FPGA + EDA + 真题实战（按真实赛制差异化）。"""
    lines = []
    lines.append("🗺️ 电子设计备赛四阶段（建议提前 3–6 个月启动；团队通常 3 人：硬件 / 软件 / 报告）：")
    lines.append("  1) 打基础（第1–6周）：吃透模拟/数字电路、单片机（STM32/51）与 C 语言；"
                 "练常用模块——电源、运算放大、ADC/DAC、电机驱动、显示/通信（串口/SPI/I2C）。")
    lines.append("  2) 专项突破（第7–12周）：攻嵌入式外设（定时器/PWM/中断/捕获）、FPGA/Verilog 基础、"
                 "传感与信号处理；用 Altium/立创 EDA 画板、焊接调试，把「模块→系统」串起来。")
    lines.append("  3) 真题实战（第13–18周）：限时做全国大学生电子设计竞赛近 5 年赛题（四天三夜真题最贴近实战），"
                 "完整跑通「方案→电路→代码→调试→写报告」；每题复盘指标是否达标、有没有更简方案。")
    lines.append("  4) 冲刺模考（赛前2–4周）：全真四天三夜模拟，整理可复用模块库与代码模板，"
                 "重点练仪器使用（示波器/信号源/电源）与现场抗压调试。")
    lines.append("")
    lines.append("📚 资料：TI 杯/瑞萨杯等赛题与官方培训、经典教材（童诗白《模拟电子技术基础》、"
                 "康华光《电子技术基础》）、立创/野火/正点原子开源例程；校电子类实验室与指导老师资源最关键。")
    lines.append("💡 提分点：省赛重「指标达成+稳定性」，报告要写清方案对比与测试数据；"
                 "把常用电路（放大/滤波/电源）做成可靠模块，赛场能省大量调试时间。")
    if profile:
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"硬件弱就主攻电路与焊接、软件弱就补嵌入式 C 与算法；三人分工明确、互补短板最稳。"
            )
    return "\n".join(lines)


def _build_engineering_prep(comp, profile) -> str:
    """机械/工程类备赛：机构设计 + 三维建模/仿真 + 样机，或智能车控制算法（按赛项细分）。"""
    lines = []
    name = comp.competition_name or ""
    is_car = any(k in name for k in ("智能汽车", "智能车", "车辆"))
    lines.append("🗺️ 机械/工程备赛四阶段（建议提前 3–6 个月启动；团队通常 3–5 人：结构 / 控制 / 文档）：")
    if is_car:
        lines.append("  （智能车方向）1) 打基础（第1–6周）：吃透单片机（STM32）、C 语言与基本控制理论（PID），"
                     "熟悉舵机/电机驱动、编码器、摄像头/电磁循迹传感器。")
        lines.append("  （智能车方向）2) 专项突破（第7–12周）：调通循迹算法（电磁/摄像头路径识别）、"
                     "速度环/转向环 PID、通信与调度；做最小小车跑通基础赛道。")
        lines.append("  （智能车方向）3) 真题实战（第13–18周）：按往届赛道元素训练——弯道/坡道/障碍/返折，"
                     "优化速度与稳定性；每轮记录参数与圈速，做对比实验。")
        lines.append("  （智能车方向）4) 冲刺模考（赛前2–4周）：全真调车连跑，整理参数表与应急方案，"
                     "重点练现场调度与突发状况处理。")
    else:
        lines.append("  （机械/结构方向）1) 打基础（第1–6周）：吃透机械原理/机械设计，熟练三维建模"
                     "（SolidWorks/Creo/UG）与工程图；了解常用材料、标准件与加工工艺。")
        lines.append("  （机械/结构方向）2) 专项突破（第7–12周）：做机构方案论证与仿真（ANSYS/ADAMS 静力·"
                     "运动·模态），把「功能→结构→强度」打通；练样机/模型制作与装配。")
        lines.append("  （机械/结构方向）3) 真题实战（第13–18周）：按赛题做 1–2 轮完整方案（含仿真报告），"
                     "对照评分标准自检；每轮复盘机构创新点与可行性。")
        lines.append("  （机械/结构方向）4) 冲刺模考（赛前2–4周）：做样机/模型终稿与答辩演练，"
                     "整理加工清单与成本，重点练方案陈述与问答。")
    lines.append("")
    lines.append("📚 资料：赛项官网章程与往届获奖作品、机械设计手册、仿真软件官方教程、"
                 "校工程训练中心/实验室与指导老师；智能车可参考往届技术报告与开源车模。")
    lines.append("💡 提分点：机械类「实物/仿真占分比」通常最高，重方案创新+可落地；"
                 "智能车重「速度与稳定兼得」，参数标定和现场调车经验决定上限。")
    if profile:
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"控制/编程熟可主攻算法与调车，机械底子好负责结构与建模；跨专业组队（机械+电控+文档）最稳。"
            )
    return "\n".join(lines)


def _build_physics_prep(comp, profile) -> str:
    """物理类备赛：实验竞赛（动手+数据处理） / 学术竞赛 CUPT（文献+对抗） / 知识赛（理论刷题）。"""
    lines = []
    name = comp.competition_name or ""
    is_cupt = "CUPT" in name or "学术" in name
    is_exp = "实验" in name
    lines.append("🗺️ 物理备赛四阶段（建议提前 3–5 个月启动；多数以校队/小组形式备赛）：")
    lines.append("  1) 补基础（第1–6周）：系统复习本科核心物理（力/热/电/光/原），"
                 "补齐数学工具（微积分/线代/微分方程）；实验类同步练仪器操作与误差分析。")
    if is_cupt:
        lines.append("  2) 专题（第7–12周）：CUPT 以「对抗辩论」为主——挑往届赛题做文献调研、理论建模、"
                     "实验验证，准备正反方陈述与提问；重点练物理直觉与口头交锋。")
    elif is_exp:
        lines.append("  2) 专题（第7–12周）：实验竞赛重「设计方案+数据处理」——练不确定度评定、"
                     "拟合与误差分析、仪器（示波器/光电/传感）使用，做往届实验题的完整报告。")
    else:
        lines.append("  2) 专题（第7–12周）：知识类赛刷真题、攻典型模型（刚体/电磁/波动/量子基础），"
                     "建立「题型→方法」映射；实验类同步补动手。")
    lines.append("  3) 真题实战（第13–18周）：限时做近 3–5 年真题/赛题，实验类完整写报告、"
                 "CUPT 做模拟对抗；每题复盘思路与表达。")
    lines.append("  4) 冲刺模考（赛前2–4周）：全真模拟（实验操作 / 对抗辩论 / 笔试），"
                 "整理常用公式与仪器清单，查漏补缺。")
    lines.append("")
    lines.append("📚 资料：赛事官网与章程、普通物理/理论力学/电磁学教材、往届 CUPT 赛题与对抗视频、"
                 "校物理实验中心与指导老师；实验类参考《大学物理实验》教材。")
    lines.append("💡 提分点：实验竞赛「数据真实+不确定度合理」最关键；CUPT 赢在「调研深+表达清+答辩稳」；"
                 "知识赛重基础扎实与解题速度。")
    if profile:
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"理论强可主攻建模与笔试、动手强负责实验、表达强负责 CUPT 陈述；按目标赛项定向补最短木板。"
            )
    return "\n".join(lines)


def _build_english_prep(comp, profile) -> str:
    """英语类备赛：演讲/写作/阅读/辩论/综合（NECCS），按赛项定向积累（词汇+输出）。"""
    lines = []
    name = comp.competition_name or ""
    is_speech = any(k in name for k in ("演讲", "口语"))
    is_writing = "写作" in name
    is_debate = "辩论" in name
    lines.append("🗺️ 英语备赛四阶段（建议提前 3–5 个月启动；重日积月累，突击难见效）：")
    lines.append("  1) 打底（第1–8周）：每天背核心词汇（按赛项难度，如专四/专八/雅思量级）+ 精听+泛读，"
                 "把「输入量」堆起来；用 APP/词书固定每日任务。")
    if is_speech or is_debate:
        lines.append("  2) 专项（第9–16周）：演讲/辩论重「逻辑+发音+台风」——写定题稿并打磨发音语调，"
                     "练即兴（抽题 3 分钟构思）；录自己回放改。")
    elif is_writing:
        lines.append("  2) 专项（第9–16周）：写作重「结构+语料」——背高频模板与衔接句式，按议论文/图表/书信等题型练，"
                     "找人批改并总结常错点（时态/冠词/搭配）。")
    else:
        lines.append("  2) 专项（第9–16周）：综合/阅读类按题型刷真题（听力/阅读/翻译/词汇语法），"
                     "建立错题本，针对性补弱项。")
    lines.append("  3) 真题实战（第17–22周）：限时做近 3–5 年外研社/NECCS 等真题，全真模拟；"
                 "每套复盘失分点。")
    lines.append("  4) 冲刺模考（赛前2–4周）：按赛制做 1–2 轮全真演练（演讲脱稿/写作限时/辩论对抗），"
                 "整理高频话题与模板，调整状态。")
    lines.append("")
    lines.append("📚 资料：赛事官网章程与样题、外研社赛事平台、历年真题、《新概念/专四专八写作》、"
                 "TED/播客精听材料；校外语角与往年获奖选手经验最实用。")
    lines.append("💡 提分点：演讲/辩论「内容与台风各半」，写作「结构清晰+少错」胜出；"
                 "综合赛（NECCS）靠词汇量与熟练度，日常积累比临时突击有效得多。")
    if profile:
        major = getattr(profile, "major", None)
        skills = getattr(profile, "skills", None) or []
        if major or skills:
            lines.append(
                f"\n💡 结合你的画像（{major or '专业未填'} / 技能：{', '.join(skills) or '待补充'}）："
                f"词汇弱先堆输入量、输出弱（说/写）多练限时产出；按你目标赛项（演讲/写作/综合）定向投入时间。"
            )
    return "\n".join(lines)


def _build_eval_answer(comp, web_results, profile) -> str:
    """对「含金量 / 难不难 / 值不值得」给出平衡研判。"""
    name = comp.competition_name
    year = comp.document_year
    title = name if str(year) in str(name) else f"{name}（{year}）"
    lines = [f"关于「{title}」值不值得参加、难度如何，我给你一个平衡的判断框架：\n"]
    lines.append("✅ 它的价值通常在这些方面：")
    lines.append("  · 简历/综测：作为受认可的学科竞赛，获奖在保研、综测、就业简历上有实质加分；")
    lines.append("  · 能力成长：逼着自己系统补短板（算法/工程/文档/答辩），比零散自学更成体系；")
    lines.append("  · 圈层资源：通过校赛/省赛认识同好与指导老师，打开后续项目与实习机会。")
    lines.append("")
    lines.append("⚠️ 也要客观看难度与成本：")
    lines.append("  · 越是「认可度高」的赛事，省赛以上竞争越激烈，需要持续投入（通常 3–6 个月）而非临时突击；")
    if _is_programming_comp(comp):
        lines.append("  · 软件/算法类对算法功底要求高，零基础直接冲国奖不现实，建议从省赛获奖率较高的赛项切入；")
    elif _is_math_modeling_comp(comp):
        lines.append("  · 数学建模重团队分工与论文表达，跨专业三人组（建模/编程/写作）比单打独斗走得更远；摘要写不好连国奖门槛都难进；")
    elif _is_innovation_comp(comp):
        lines.append("  · 创新创业类非常看重「真实落地数据+知识产权」，纯点子空想很难走到省赛以上；跨专业组队（技术+商科+设计）胜率明显更高；")
    elif _is_electronics_comp(comp):
        lines.append("  · 电子设计类软硬件结合，既考电路/嵌入式功底也考四天三夜真题实战的抗压与调试能力，建议从校赛/省赛真题入手、先搭稳常用模块库；")
    elif _is_engineering_comp(comp):
        lines.append("  · 机械/工程类重方案论证与样机/模型落地，跨专业组队（机械+控制+文档）更稳，实物/仿真往往占分比最高；")
    elif _is_physics_comp(comp):
        lines.append("  · 物理类分「实验/学术」两条线：实验竞赛重动手与数据处理，CUPT 重文献调研与对抗辩论，纯知识赛重理论刷题，路线差异大；")
    elif _is_english_comp(comp):
        lines.append("  · 英语类赛制差异大（演讲/写作/阅读/辩论/综合），重日积月累的词汇与输出能力，突击难见效，建议按目标赛项定向练；")
    lines.append("  · 投入产出比取决于你的目标：若只为综测凑数，优先选与本专业/技能最贴合的赛项，性价比最高。")
    lines.append("")
    lines.append("🎯 适合谁：")
    lines.append("  · 想保研/刷简历、且愿意花时间系统训练的同学；")
    if comp.eligible_students:
        lines.append(f"  · 参赛对象覆盖：{ '、'.join(comp.eligible_students) }，先确认你在本科学段内；")
    lines.append("  · 时间紧的同学，建议「保一门主力赛项 + 顺带了解其他」，别贪多。")
    web = _web_block(web_results, limit=3)
    if web:
        lines.append(web)
    lines.append("\nℹ️ 以上为通用研判，具体获奖比例/含金量以当年官方章程与你所在院校认定为准。")
    return "\n".join(lines)


def _build_selection_answer(comp, profile) -> str:
    """对「怎么选赛项 / 报哪个」给出决策思路。"""
    name = comp.competition_name
    lines = [f"「{name}」怎么选赛项/方向，给你一个决策思路：\n"]
    if comp.required_skills:
        lines.append("· 先看赛项清单：" + "、".join(comp.required_skills)
                     + "。挑与你当前最强技能重合度最高的，起步最快、性价比最高。")
    if profile and getattr(profile, "skills", None):
        lines.append(f"· 结合你的技能（{', '.join(profile.skills)}）：优先用熟的语言/方向做主力，"
                     "把陌生但高分的专题列为「下一步突破」。")
    lines.append("· 时间与目标：保研/简历导向 → 选认可度高、与你专业贴合的赛项；纯练手 → 选最感兴趣、能坚持下来的。")
    lines.append("· 实在拿不准，可以说「推荐适合我的比赛」，我按你的画像帮你匹配；"
                 "或对比多场时说「A 和 B 怎么选」让我帮你拆解。")
    return "\n".join(lines)


def _compose_chat_deterministic(state, comp, citations, web_results) -> Optional[str]:
    """LLM 未启用或生成失败时的确定性兜底；按问题类型产出真正有用的回答。

    返回 None 表示「无确定模板可套」（交由 compose 的通用兜底处理）。
    """
    if comp is None:
        return None
    q = state.get("question") or ""
    profile = _load_profile(state)
    if _is_prep_question(q):
        if "一周" in q or "7天" in q or "七天" in q:
            body = (f"「{comp.competition_name}（{comp.document_year}）」一周准备建议（不是官方赛程）：\n"
                    "第1天：阅读官网规则，确认个人资格、人数、截止和材料。\n"
                    "第2天：盘点现有技能与样题，确定目标和分工。\n"
                    "第3至4天：完成最小可运行方案或练习，记录问题与结果。\n"
                    "第5天：补短板并复核结果，整理文档。\n"
                    "第6天：按官方材料要求做一次完整演练。\n"
                    "第7天：校对材料、备份并核对官网最新通知；不把建议日期当作官方截止。")
        else:
            body = _build_prep_guide(comp, web_results, profile)
    elif _is_eval_question(q):
        body = _build_eval_answer(comp, web_results, profile)
    elif _is_selection_question(q):
        body = _build_selection_answer(comp, profile)
    else:
        return None
    answer = body + _pending_review_notice(state, "chat") + _citations_appendix(citations)
    if state.get("version_resolution_note"):
        answer = state["version_resolution_note"] + "\n\n" + answer
    return answer


def _generate_grounded(state: AgentState, intent: str, citations: list, name: str,
                       allow_empty_evidence: bool = False) -> Optional[str]:
    """调用 LLM 基于证据自由撰写；LLM 未启用/生成失败返回 None（回退确定性）。"""
    if not citations and not allow_empty_evidence:
        return None
    brief = _build_evidence_brief(citations, state.get("web_results"))
    profile = _load_profile(state)
    profile_summary = _profile_summary(profile) if profile else ""
    question = state.get("question") or ""
    gate = state.get("gate") or {}
    brief += (f"\n【系统锁定赛事】{name or '未锁定，不得替用户确定赛事'}\n"
              f"【当前业务日期】{contest_now().isoformat()}\n"
              f"【资格边界】eligible={gate.get('eligible')}；原因={gate.get('reasons', [])}；"
              f"候选信息={bool(state.get('pending_review'))}。不得输出与系统相反的资格判断；"
              "候选信息不能确定资格或匹配分数。个人画像不是官方证据。")
    out = _llm_generate(
        question, brief,
        intent=intent,
        profile_summary=profile_summary,
        model=state.get("model"),
    )
    if not out:
        return None
    if state.get("pending_review") or gate.get("eligible") is False:
        if re.search(r"(?:你|您|你们|您们).{0,8}(?:符合|满足)(?:报名|参赛|硬性)|(?:可以|能)报名|可以参赛", out):
            return None
    if any(int(n) > len(citations) or int(n) < 1 for n in re.findall(r"\[(\d+)\]", out)):
        return None
    return _sanitize_citations(out, len(citations))


def _pending_review_notice(state: AgentState, intent: str) -> str:
    """把内部待审核状态转成用户可见的可信边界。

    「单个事实已有官方原文」与「整体赛事证据已达推荐门槛」是两件事：
    前者可以照常展示引用，后者未满足时仍必须禁止资格结论与推荐评分。
    """
    if not state.get("pending_review") or intent not in ("qa", "detail", "chat"):
        return ""
    resolved_id = state.get("resolved_competition")
    comp = db.get_competition(resolved_id) if resolved_id else None
    if comp is not None and comp.official_source_status == "found":
        if not state.get("citations"):
            return (
                "\n\nℹ️ 可信边界：该赛事已收录官方来源链接，但缺少可确认本次问题的官方原文，"
                "目前仅作为候选信息；不构成资格判断或推荐评分，请以官网最新通知为准。"
            )
        return (
            "\n\nℹ️ 可信边界：上述事实已关联官方原文，但该赛事的关键字段证据尚未齐全，"
            "目前仅作为候选信息；不构成资格判断或推荐评分，请以官网最新通知为准。"
        )
    return (
        "\n\n该赛事目前仅作为候选信息展示，关键字段证据尚不完整；"
        "以上内容不构成资格判断或匹配评分，请以官网最新通知为准。"
    )


def _insert_notice_before_citations(answer: str, notice: str) -> str:
    """让可信边界出现在引用附录之前。

    前端会隐藏文本引用附录并改用可交互证据卡；如果把风险提示放在附录之后，
    提示也会被一起隐藏。
    """
    if not notice:
        return answer
    marker = "\n\n——— 引用来源 ———"
    if marker in answer:
        return answer.replace(marker, notice + marker, 1)
    return answer + notice


def _finalize_grounded(generated: str, state: AgentState, citations: list,
                       intent: str, trace: list) -> str:
    """把 LLM 生成结果 + 确定性门控/评分结论 + 引用附录拼成最终答案。"""
    answer = generated
    # chat 为开放/建议类，不追加任何「门控」结论（与备赛/研判建议无关）
    if intent != "chat":
        gate = state.get("gate") or {}
        if gate.get("source_issues"):
            # 由 _pending_review_notice 统一说明「单个事实可引用，整体不评分」的边界。
            pass
        elif gate.get("eligible") is False:
            reasons = gate.get("reasons") or []
            if reasons:
                answer += f"\n\n⚠️ 结合你的画像，你暂不符合该赛事硬性条件：{'；'.join(reasons)}"
            else:
                answer += "\n\nℹ️ 说明：该赛事官方来源尚待核验，报名与赛程请以官网最新通知为准。"
        elif gate.get("eligible") is True and not (state.get("score") or {}).get("skipped"):
            answer += f"\n\n✅ 你符合报名硬性条件，综合匹配度约 {(state.get('score') or {}).get('total')} 分。"
    answer += _pending_review_notice(state, intent)
    answer += _citations_appendix(citations)
    if state.get("version_resolution_note"):
        answer = state["version_resolution_note"] + "\n\n" + answer
    trace.append("组装答案：LLM 基于证据自由撰写（带 [n] 引用）")
    return answer


# ---------------------------------------------------------------------------
# 节点 7：组装答案（自动带引用）
# ---------------------------------------------------------------------------
def node_compose(state: AgentState) -> AgentState:
    trace = list(state.get("trace") or [])
    intent = state.get("intent") or "unknown"
    citations = state.get("citations") or []
    name = state.get("resolved_name")

    # 歧义澄清优先：多版本未定 / 需明确年份时，直接给出澄清，不进入自由生成，
    # 避免 LLM 在信息不足时自行猜测赛事版本。
    if state.get("clarification"):
        answer = state["clarification"]
        answer += _citations_appendix(citations)
        trace.append("组装答案：歧义澄清（需用户明确年份/版本）")
        return {**state, "answer": answer, "trace": trace}

    requested = _requested_fields(state.get("question") or "")
    if intent == "qa" and requested:
        comp = db.get_competition(state.get("resolved_competition")) if state.get("resolved_competition") else None
        lines = []
        quote_lines = {}
        for field in sorted(requested):
            matching = [(i, c) for i, c in enumerate(citations) if c.get("field") == field]
            if field == "registration_fee":
                matching = [(i, c) for i, c in enumerate(citations)
                            if re.search(r"参赛费|报名费|收费|费用|免费", c.get("source_text", ""))]
            if matching:
                for i, citation in matching:
                    if field == "registration_deadline" and comp and comp.registration_deadline:
                        lines.append(f"报名截止时间：{format_deadline(comp)}{_cite_marker(i)}。")
                    else:
                        text = citation["source_text"]
                        if text in quote_lines:
                            lines[quote_lines[text]] += _cite_marker(i)
                        else:
                            quote_lines[text] = len(lines)
                            lines.append(f"{text}{_cite_marker(i)}")
            elif field in ("registration_fee", "registration_deadline", "competition_end_date", "required_materials"):
                labels = {"registration_fee": "报名费用", "registration_deadline": "报名截止",
                          "competition_end_date": "比赛结束时间", "required_materials": "材料要求"}
                refusal = "不能确认截止时间" if field == "registration_deadline" else "无法确认"
                lines.append(f"{labels[field]}缺少官方原文证据，{refusal}，请查看官网最新通知。")
        if lines:
            answer = _finalize_grounded("\n".join(dict.fromkeys(lines)), state, citations, intent, trace)
            trace[-1] = "组装答案：按所问字段返回官方原文与唯一结构化日期"
            return {**state, "answer": answer, "trace": trace}

    # Critical fact answers are deterministic; the model handles explanations and advice.
    if intent in ("qa", "detail") and citations:
        generated = _generate_grounded(state, intent, citations, name)
        if generated:
            answer = _finalize_grounded(generated, state, citations, intent, trace)
            return {**state, "answer": answer, "trace": trace}
    if intent == "chat":
        if any(w in (state.get("question") or "") for w in ("一周", "7天", "七天")):
            comp = db.get_competition(state.get("resolved_competition")) if state.get("resolved_competition") else None
            det = _compose_chat_deterministic(state, comp, citations, state.get("web_results") or [])
            if det:
                trace.append("组装答案：按一周期限生成通用准备建议")
                return {**state, "answer": det, "trace": trace}
        # 优先用 LLM 当参谋；未启用/失败时回退到确定性生成器（备赛/研判/选型也能真正回答）。
        generated = _generate_grounded(state, "chat", citations, name, allow_empty_evidence=True)
        if generated:
            answer = _finalize_grounded(generated, state, citations, "chat", trace)
            return {**state, "answer": answer, "trace": trace}
        # —— LLM 未启用或生成失败：走确定性生成，确保开放/建议类问题有实质回答 ——
        resolved_id = state.get("resolved_competition")
        det = _compose_chat_deterministic(
            state,
            db.get_competition(resolved_id) if resolved_id else None,
            citations,
            state.get("web_results") or [],
        )
        if det:
            trace.append("组装答案：chat 确定性生成（备赛/研判/选型）")
            return {**state, "answer": det, "trace": trace}
        # 完全兜底：仅当无可套用模板时，列出已知赛事事实并引导启用模型。
        if citations:
            lines = ["我先基于已知赛事信息给你方向性参考：\n"]
            for i, c in enumerate(citations[:5]):
                txt = (c.get("source_text") or "").strip().replace("\n", " ")
                if len(txt) > 80:
                    txt = txt[:80] + "…"
                lines.append(f"· {txt}{_cite_marker(i)}")
            lines.append("\n如需就备赛规划、能力提升等开放问题获得更自然的对话式建议，可在设置中启用智能模型。")
            answer = "\n".join(lines)
        else:
            answer = (
                "我可以帮你查询具体赛事的规则、给出个性化推荐或生成组队文案。"
                "请告诉我具体赛事名称（例如「蓝桥杯报名截止日期」），或直接说「推荐适合我的比赛」。"
                "启用智能模型后，这里还能就备赛规划、能力提升等开放问题给出建议。"
            )
        answer += _pending_review_notice(state, "chat") + _citations_appendix(citations)
        if state.get("version_resolution_note"):
            answer = state["version_resolution_note"] + "\n\n" + answer
        trace.append("组装答案：chat 回退提示（基于证据引导）")
        return {**state, "answer": answer, "trace": trace}

    if intent == "teammate":
        matches = state.get("teammate_matches") or []
        if not matches:
            answer = (
                "暂时没有可供匹配的队友候选。你可以先完善个人画像（专业、技能、年级），"
                "系统会从队友库中为你推荐互补搭档。"
            )
        else:
            lines = ["根据你的画像，我为你匹配了以下互补队友（按互补度排序）：\n"]
            for i, m in enumerate(matches, 1):
                name = m.get("display_name") or m.get("user_id")
                lines.append(
                    f"{i}. {name}（{m.get('major')} · {m.get('grade')}）— 互补度 {m.get('match_score'):.0f} 分"
                )
                for r in (m.get("reasons") or [])[:3]:
                    lines.append(f"   · {r}")
            lines.append("\n可在「我的队友」中查看完整画像与联系方式（演示环境为虚拟候选）。")
            answer = "\n".join(lines)
        trace.append("组装答案：teammate 互补队友推荐")
        return {**state, "answer": answer, "trace": trace}

    if intent == "team":
        body = state.get("team_copy") or "未能生成组队文案。"
        answer = body + _citations_appendix(citations)

    elif intent == "recommend":
        recos = state.get("recommendations") or []
        if not recos:
            answer = "暂时没有通过官网来源、关键证据和报名时间门控的正式推荐。请检查个人画像，或前往「赛事大厅」查看待核验候选并自行核对官网。"
        else:
            status_map = {
                "highly_suitable": "高度适合", "suitable": "比较适合",
                "marginal": "可参加需补充", "not_prioritized": "暂不优先",
            }
            lines = ["根据你的画像，门控+评分后为你推荐以下赛事（已过滤关键证据不完整、不符合硬性资格以及不可报名的赛事）：\n"]
            for i, r in enumerate(recos, 1):
                mark = _cite_marker(r["cite_index"]) if r.get("cite_index") is not None else ""
                tags = []
                if r.get("urgent"):
                    tags.append("⏰临近截止")
                tag_s = ("（" + "，".join(tags) + "）") if tags else ""
                sc = f"{r['score']}分" if r.get("score") is not None else "—"
                lines.append(
                    f"{i}. {r['competition_name']} · {status_map.get(r['recommendation_status'], r['recommendation_status'])} · {sc}{mark}{tag_s}"
                )
            lines.append("\n以上结论基于已锚定的官方来源，报名前仍请查看赛事官网最新通知。")
            answer = "\n".join(lines)
        answer += _citations_appendix(citations)

    elif intent in ("qa", "detail") and _asks_registration_deadline(state.get("question") or ""):
        comp = db.get_competition(state.get("resolved_competition")) if state.get("resolved_competition") else None
        mark = _cite_marker(0) if citations else ""
        if comp is None:
            answer = "没能定位到具体赛事，请补充赛事名称。"
        elif not citations:
            answer = f"「{comp.competition_name}」的报名截止字段缺少官方原文证据，不能确认截止时间。请查看官网最新通知。"
        elif comp.registration_deadline is None:
            answer = f"「{comp.competition_name}」的已存官方原文未能确认全国统一报名截止日期{mark}，请查看官网最新通知。"
        else:
            answer = f"「{comp.competition_name}」的报名截止时间：{format_deadline(comp)}{mark}。"
        answer += _citations_appendix(citations)

        gate = state.get("gate") or {}
        if gate.get("source_issues"):
            pass
        elif gate.get("eligible") is False:
            reasons = gate.get("reasons") or []
            if reasons:
                answer += f"\n\n⚠️ 结合你的画像，你暂不符合该赛事硬性条件：{'；'.join(reasons)}"
            else:
                answer += "\n\nℹ️ 说明：该赛事官方来源尚待核验，报名与赛程请以官网最新通知为准。"
        elif gate.get("eligible") is True and not (state.get("score") or {}).get("skipped"):
            answer += f"\n\n✅ 你符合报名硬性条件，综合匹配度约 {(state.get('score') or {}).get('total')} 分。"

    elif intent in ("qa", "detail"):
        if not citations:
            answer = (
                (f"未在「{name}」的资料中检索到与你问题相关的条款。" if name
                 else "没能定位到你想了解的具体赛事，请补充赛事名称，例如「蓝桥杯 团队几人」。")
            )
        else:
            head = f"关于「{name}」，依据官方通知检索到：\n" if name else "依据官方通知检索到：\n"
            lines = [head]
            for i, c in enumerate(citations):
                txt = (c.get("source_text") or "").strip().replace("\n", " ")
                lines.append(f"· {txt}{_cite_marker(i)}")
            gate = state.get("gate") or {}
            if gate.get("source_issues"):
                pass
            elif gate.get("eligible") is False:
                reasons = gate.get("reasons") or []
                if reasons:
                    lines.append(f"\n⚠️ 结合你的画像，你暂不符合该赛事硬性条件：{'；'.join(reasons)}")
                else:
                    lines.append("\nℹ️ 说明：该赛事官方来源尚待核验，报名与赛程请以官网最新通知为准。")
            elif gate.get("eligible") is True and not (state.get("score") or {}).get("skipped"):
                sc = state.get("score") or {}
                lines.append(f"\n✅ 你符合报名硬性条件，综合匹配度约 {sc.get('total')} 分。")
            answer = "\n".join(lines) + _citations_appendix(citations)
        answer = _llm_polish(answer, f"赛事={name}；证据条数={len(citations)}", model=state.get("model"))

    else:
        answer = state.get("clarification") or (
            "我可以帮你查询赛事规则、给出个性化推荐或生成组队文案。请补充具体赛事名称。"
        )

    if state.get("version_resolution_note"):
        answer = state["version_resolution_note"] + "\n\n" + answer
    answer = _insert_notice_before_citations(answer, _pending_review_notice(state, intent))

    # 联网补充：结果存于 state["web_results"]，由前端明确标注为「🌐 联网信息（仅供参考）」
    # 并附可点击来源链接；不混入答案正文的官方 [n] 引用，保持证据一致性。

    trace.append("组装答案：完成")
    return {**state, "answer": answer, "trace": trace}


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


def _branch(state: AgentState) -> str:
    return state.get("intent") or "unknown"


def build_graph():
    """构建并编译 LangGraph 闭环（真实 langgraph 或内置等价引擎）。"""
    global _USING_LANGGRAPH
    g = StateGraph(AgentState)

    # 节点包装器统一记录耗时、证据数量、数据版本、业务摘要与兜底状态。
    # 原节点继续只负责业务逻辑，避免可观测性代码侵入推荐与证据规则。
    g.add_node("route_intent", _instrument_node("route_intent", node_route_intent))
    g.add_node("retrieve", _instrument_node("retrieve", node_retrieve))
    g.add_node("gate", _instrument_node("gate", node_gate))
    g.add_node("score", _instrument_node("score", node_score))
    g.add_node("team_copy", _instrument_node("team_copy", node_team_copy))
    g.add_node("teammate_match", _instrument_node("teammate_match", node_teammate_match))
    g.add_node("recommend_all", _instrument_node("recommend_all", node_recommend_all))
    g.add_node("web_augment", _instrument_node("web_augment", node_web_augment))
    g.add_node("compose", _instrument_node("compose", node_compose))

    g.add_edge(START, "route_intent")

    # 意图分流
    g.add_conditional_edges(
        "route_intent",
        _branch,
        {
            "qa": "retrieve",
            "detail": "retrieve",
            "team": "retrieve",
            "chat": "retrieve",
            "teammate": "teammate_match",
            "recommend": "recommend_all",
            "unknown": "compose",
        },
    )

    # qa/detail：检索 → 联网补充 → 门控 → 评分 → 组装
    # team：      检索 → 联网补充 → 门控 → 评分 → 组队文案 → 组装
    g.add_edge("retrieve", "web_augment")
    g.add_edge("web_augment", "gate")
    g.add_edge("gate", "score")

    # score 之后根据意图决定是否生成组队文案
    def _after_score(state: AgentState) -> str:
        return "team_copy" if state.get("intent") == "team" else "compose"

    g.add_conditional_edges("score", _after_score, {"team_copy": "team_copy", "compose": "compose"})
    g.add_edge("team_copy", "compose")
    g.add_edge("teammate_match", "compose")
    g.add_edge("recommend_all", "compose")
    g.add_edge("compose", END)

    compiled = g.compile()
    print(f"[agent] 闭环图已编译，引擎={'langgraph' if _USING_LANGGRAPH else '内置等价引擎(shim)'}")
    return compiled


# 编译后的单例（供 API 复用）
_GRAPH = None


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def _summarize_trace(
    trace: list[AuditTraceStep],
    *,
    total_duration_ms: float,
    citations: list[dict],
    pending_review: bool,
    error: Optional[str],
    data_version: str,
) -> tuple[dict, dict]:
    """生成适合前端概览与工程指标面板的审计摘要。"""
    status_counts = {
        status: sum(1 for step in trace if step["status"] == status)
        for status in ("succeeded", "skipped", "degraded", "failed")
    }
    fallback_count = sum(1 for step in trace if step["fallback"])
    if status_counts["failed"] or error:
        overall_status = "failed"
    elif status_counts["degraded"] or fallback_count or pending_review:
        overall_status = "degraded"
    else:
        overall_status = "succeeded"

    trace_summary = {
        "status": overall_status,
        "total_steps": len(trace),
        "succeeded_steps": status_counts["succeeded"],
        "skipped_steps": status_counts["skipped"],
        "degraded_steps": status_counts["degraded"],
        "failed_steps": status_counts["failed"],
        "fallback_steps": fallback_count,
        "evidence_count": len(citations),
        "path": [step["node"] for step in trace],
        "labels": [step["label"] for step in trace],
    }
    metrics = {
        "status": overall_status,
        "total_duration_ms": round(max(0.0, total_duration_ms), 3),
        "node_duration_ms": round(sum(step["duration_ms"] for step in trace), 3),
        "step_count": len(trace),
        "evidence_count": len(citations),
        "evidence_fields": sorted(
            {str(citation.get("field")) for citation in citations if citation.get("field")}
        ),
        "fallback_count": fallback_count,
        "pending_review": pending_review,
        "data_version": data_version,
        "engine": "langgraph" if _USING_LANGGRAPH else "shim",
    }
    return trace_summary, metrics


def run_agent(
    question: str,
    user_id: Optional[str] = None,
    competition_id: Optional[str] = None,
    top_k: int = 4,
    model: Optional[str] = None,
    context_competition_id: Optional[str] = None,
    task_mode: bool = False,
) -> dict:
    """一次问答，走完整闭环，返回可直接序列化的结果字典。

    ``model`` 可选：请求级覆盖默认 LLM 模型（前端模型选择器透传），
    仅在已配置 LLM key 时生效；未配置或调用失败自动回退确定性模板。
    """
    if task_mode:
        from agent.planner import run_planning_task
        return run_planning_task(question, user_id=user_id, competition_id=competition_id, model=model)
    run_id = f"agent_{uuid4().hex}"
    started = perf_counter()

    # 入口前置拦截：纯符号/空白噪声（如 "???""、乱敲字符）无可理解语义，
    # 直接引导重述，避免套用画像产出无意义闲聊（压测 c11 改进项）。
    if not _is_meaningful_question(question):
        dv = _catalog_data_version()
        noise_trace = [{
            "node": "input-guard",
            "label": "无意义输入前置拦截",
            "status": "ok",
            "duration_ms": 0.0,
            "evidence_count": 0,
            "data_version": dv,
            "detail": "输入仅含符号/空白，无可见汉字/数字/字母，引导用户重述",
            "fallback": False,
        }]
        return {
            "run_id": run_id,
            "question": question,
            "intent": "noise",
            "resolved_competition": None,
            "resolved_name": None,
            "answer": (
                "没太看懂你的问题～换个说法我帮你查赛事。"
                "例如「蓝桥杯报名截止日期」或「推荐适合我的比赛」。"
            ),
            "citations": [],
            "web_results": [],
            "gate": {},
            "score": {},
            "recommendations": [],
            "teammate_matches": [],
            "pending_review": False,
            "data_version": dv,
            "trace": noise_trace,
            "trace_legacy": [noise_trace[0]["detail"]],
            "trace_summary": noise_trace,
            "metrics": {"total_duration_ms": 0.0, "node_count": 1, "evidence_count": 0},
            "error": None,
        }

    init: AgentState = {
        "question": question,
        "run_id": run_id,
        "user_id": user_id,
        "competition_id": competition_id,
        "context_competition_id": context_competition_id,
        "top_k": top_k,
        "model": model,
        "data_version": _catalog_data_version(),
        "trace": [],
        "citations": [],
        "web_results": [],
        "pending_review": False,
    }
    try:
        final = get_graph().invoke(init)
    except Exception as exc:  # noqa: BLE001 - 图级最后一道保守兜底
        data_version = _state_data_version(init)
        final = {
            **init,
            "answer": "本次处理未能完整完成，系统已进入保守兜底；请稍后重试或查看官方来源。",
            "error": f"orchestrator:{exc.__class__.__name__}",
            "pending_review": True,
            "trace": [
                {
                    "node": "orchestrator",
                    "label": "Agent 流程编排",
                    "status": "failed",
                    "duration_ms": round(max(0.0, (perf_counter() - started) * 1000), 3),
                    "evidence_count": 0,
                    "data_version": data_version,
                    "detail": f"流程编排异常，已进入保守兜底（{exc.__class__.__name__}）",
                    "fallback": True,
                }
            ],
        }

    trace = _normalize_existing_trace(list(final.get("trace") or []), final)
    citations = final.get("citations") or []
    pending_review = bool(final.get("pending_review"))
    data_version = _state_data_version(final)
    trace_summary, metrics = _summarize_trace(
        trace,
        total_duration_ms=(perf_counter() - started) * 1000,
        citations=citations,
        pending_review=pending_review,
        error=final.get("error"),
        data_version=data_version,
    )
    return {
        "run_id": run_id,
        "question": question,
        "intent": final.get("intent"),
        "resolved_competition": final.get("resolved_competition"),
        "resolved_name": final.get("resolved_name"),
        "answer": final.get("answer"),
        "citations": citations,
        "gate": final.get("gate") or {},
        "score": final.get("score") or {},
        "recommendations": final.get("recommendations") or [],
        "teammate_matches": final.get("teammate_matches") or [],
        "pending_review": pending_review,
        "data_version": data_version,
        "trace": trace,
        # 同步保留旧版字符串轨迹，供脚本或尚未升级的调用方平滑迁移。
        "trace_legacy": [step["detail"] for step in trace],
        "trace_summary": trace_summary,
        "metrics": metrics,
        "web_results": final.get("web_results") or [],
        "error": final.get("error"),
    }


if __name__ == "__main__":
    # 自测：四类意图闭环
    print("=" * 70)
    samples = [
        ("蓝桥杯团队几人？报名什么时候截止", None, None),
        ("介绍一下数学建模竞赛", None, None),
        ("推荐适合我的比赛", "mock_user_001", None),
        ("帮我写一份蓝桥杯的组队招募文案", "mock_user_001", None),
        ("今天天气怎么样", None, None),
    ]
    for q, uid, cid in samples:
        r = run_agent(q, user_id=uid, competition_id=cid)
        print(f"\n【问】{q}  (user={uid})")
        print(f"意图={r['intent']}  赛事={r['resolved_name']}  引用={len(r['citations'])} 条  待确认={r['pending_review']}")
        print("轨迹：" + " → ".join(r["trace_legacy"]))
        print("答：\n" + (r["answer"] or ""))
        print("-" * 70)
