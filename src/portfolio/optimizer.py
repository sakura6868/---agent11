"""在资格、时间与精力约束下生成可解释的赛事组合方案。"""

from __future__ import annotations

import itertools
import math
from datetime import date, datetime, timezone

from recommendation.engine import estimate_competition_workload, recommend_for_user
from contest_clock import calendar_date, contest_now
from schemas import (
    Competition,
    PortfolioItem,
    PortfolioOptimizeResponse,
    PortfolioPlan,
    PortfolioPreferences,
    RoadmapEdge,
    RoadmapNode,
    UserProfile,
    UserProject,
)


_MODES = {
    "steady": {"label": "稳妥型", "capacity_ratio": 0.60, "max_cap": 2, "conflict_penalty": 28.0},
    "balanced": {"label": "均衡型", "capacity_ratio": 0.80, "max_cap": 3, "conflict_penalty": 17.0},
    "sprint": {"label": "冲刺型", "capacity_ratio": 0.95, "max_cap": 4, "conflict_penalty": 8.0},
}


def _deadline(comp: Competition):
    return comp.submission_deadline or comp.registration_deadline


def _personal_effort(user: UserProfile, comp: Competition) -> float:
    """把项目总工作量换算为个人投入，明确属于系统估算。"""
    total = estimate_competition_workload(comp)
    if not comp.team_required:
        return total
    team_size = max(1, min(user.expected_team_size, comp.team_max or user.expected_team_size))
    # 团队协作并非线性分摊，保留约 20% 沟通协调成本。
    return round(total * (0.20 + 0.80 / team_size), 1)


def _weekly_curve(total_hours: float, comp: Competition, current: date, horizon: int) -> list[float]:
    deadline = _deadline(comp)
    if deadline is None:
        active = horizon
    else:
        active = max(1, min(horizon, math.ceil(max(1, (deadline - current).days) / 7)))
    # 越接近截止投入越高；权重归一化后总和仍等于估算总工时。
    weights = [0.65 + 0.70 * (i + 1) / active for i in range(active)]
    scale = total_hours / sum(weights)
    curve = [round(w * scale, 2) for w in weights]
    return curve + [0.0] * (horizon - active)


def _skill_gain(user: UserProfile, comp: Competition) -> float:
    if not comp.required_skills:
        return 40.0
    missing = sum(1 for skill in comp.required_skills if skill not in user.skills)
    return min(100.0, missing / len(comp.required_skills) * 100.0)


def _individual_value(goal: str, match_score: float, gain: float) -> float:
    if goal == "award":
        return 0.82 * match_score + 0.18 * gain
    if goal == "growth":
        return 0.48 * match_score + 0.52 * gain
    return 0.68 * match_score + 0.32 * gain


def _deadline_conflict(a: Competition, b: Competition) -> bool:
    da, db = _deadline(a), _deadline(b)
    return bool(da and db and abs((da - db).days) <= 7)


def _existing_load(
    user: UserProfile,
    projects: list[UserProject],
    competitions: dict[str, Competition],
    current: date,
    horizon: int,
) -> tuple[list[float], set[str]]:
    load = [0.0] * horizon
    existing_ids: set[str] = set()
    for project in projects:
        if project.status.value == "completed":
            continue
        comp = competitions.get(project.competition_id)
        if comp is None:
            continue
        existing_ids.add(comp.competition_id)
        # 已完成的任务不应继续占用未来容量；按项目清单剩余比例折算，
        # 同时保留 10% 协调/收尾量，避免把进行中的项目误算成零投入。
        items = list(project.items or [])
        if items:
            finished = sum(1 for item in items if item.status.value in {"done", "skipped"})
            remaining_ratio = max(0.10, (len(items) - finished) / len(items))
        else:
            remaining_ratio = 1.0
        curve = _weekly_curve(round(_personal_effort(user, comp) * remaining_ratio, 1), comp, current, horizon)
        load = [round(a + b, 2) for a, b in zip(load, curve)]
    return load, existing_ids


def _build_mode_plan(
    mode: str,
    user: UserProfile,
    candidates: list[dict],
    existing_load: list[float],
    preferences: PortfolioPreferences,
) -> PortfolioPlan:
    cfg = _MODES[mode]
    weekly_capacity = float(preferences.weekly_hours_override or user.weekly_available_hours)
    usable_capacity = weekly_capacity * cfg["capacity_ratio"]
    max_items = min(preferences.max_competitions, cfg["max_cap"])

    best_combo: tuple[dict, ...] = ()
    best_value = float("-inf")
    best_load = list(existing_load)
    best_conflicts: list[str] = []

    for size in range(1, max_items + 1):
        for combo in itertools.combinations(candidates, size):
            loads = [
                existing_load[w] + sum(item["weekly_load"][w] for item in combo)
                for w in range(preferences.horizon_weeks)
            ]
            if any(value > usable_capacity + 1e-9 for value in loads):
                continue
            conflict_pairs = [
                (a, b)
                for a, b in itertools.combinations(combo, 2)
                if _deadline_conflict(a["competition"], b["competition"])
            ]
            diversity = max(0, len({item["competition"].category for item in combo}) - 1) * 5.0
            value = sum(item["value"] for item in combo) + diversity
            value -= len(conflict_pairs) * cfg["conflict_penalty"]
            if value > best_value:
                best_value = value
                best_combo = combo
                best_load = loads
                best_conflicts = [
                    f"{a['competition'].competition_name} 与 {b['competition'].competition_name} 截止时间相距不超过7天"
                    for a, b in conflict_pairs
                ]

    if not best_combo:
        return PortfolioPlan(
            mode=mode,
            label=cfg["label"],
            capacity_hours=round(usable_capacity, 1),
            peak_weekly_load=round(max(existing_load, default=0), 1),
            utilization=round(max(existing_load, default=0) / max(usable_capacity, 1) * 100, 1),
            binding_constraints=["现有项目或候选赛事工作量已超过该策略的每周容量"],
            excluded_reasons=["可调整每周可投入时间、缩短规划数量或切换更积极的策略"],
        )

    selected_ids = {item["competition"].competition_id for item in best_combo}
    portfolio_items = []
    for item in best_combo:
        comp = item["competition"]
        portfolio_items.append(
            PortfolioItem(
                competition_id=comp.competition_id,
                competition_name=comp.competition_name,
                match_score=item["match_score"],
                estimated_total_hours=item["total_hours"],
                weekly_load=item["weekly_load"],
                deadline=_deadline(comp),
                reasons=[
                    f"个人匹配度 {item['match_score']:.1f}",
                    f"预计个人投入 {item['total_hours']:.1f} 小时（系统估算）",
                    f"技能成长潜力 {item['gain']:.1f}",
                ],
            )
        )

    peak = max(best_load, default=0.0)
    utilization = peak / max(usable_capacity, 1.0) * 100.0
    risk = min(100.0, utilization * 0.72 + len(best_conflicts) * 14.0)
    binding = []
    if utilization >= 85:
        binding.append(f"峰值周投入已达到可用容量的 {utilization:.0f}%")
    if len(best_combo) == max_items:
        binding.append(f"已达到该策略最多 {max_items} 场赛事的限制")
    excluded = []
    for item in candidates:
        comp = item["competition"]
        if comp.competition_id in selected_ids:
            continue
        reason = "组合价值低于已选方案"
        if any(_deadline_conflict(comp, chosen["competition"]) for chosen in best_combo):
            reason = "与已选赛事截止时间过近"
        excluded.append(f"{comp.competition_name}：{reason}")
        if len(excluded) >= 5:
            break

    return PortfolioPlan(
        mode=mode,
        label=cfg["label"],
        items=portfolio_items,
        total_value=round(best_value, 1),
        capacity_hours=round(usable_capacity, 1),
        peak_weekly_load=round(peak, 1),
        utilization=round(utilization, 1),
        risk_score=round(risk, 1),
        conflicts=best_conflicts,
        binding_constraints=binding,
        excluded_reasons=excluded,
        roadmap_nodes=_roadmap_nodes(user, [item["competition"] for item in best_combo], mode, date.today()),
        roadmap_edges=_roadmap_edges(user, [item["competition"] for item in best_combo]),
    )


def _roadmap_nodes(
    user: UserProfile,
    competitions: list[Competition],
    mode: str,
    current: date,
) -> list[RoadmapNode]:
    nodes: list[RoadmapNode] = []
    seen: set[str] = set()
    for comp in competitions:
        comp_node = f"competition:{comp.competition_id}"
        missing = [skill for skill in comp.required_skills if skill not in user.skills]
        nodes.append(
            RoadmapNode(
                node_id=comp_node,
                node_type="competition",
                label=comp.competition_name,
                status="not_ready" if missing else "in_progress",
                competition_id=comp.competition_id,
                due_date=_deadline(comp),
                reason=(f"尚缺 {len(missing)} 项能力" if missing else f"{mode} 路线已通过能力门槛"),
            )
        )
        for skill in comp.required_skills[:5]:
            kind = "skill" if skill in user.skills else "gap"
            node_id = f"{kind}:{skill}"
            if node_id not in seen:
                nodes.append(
                    RoadmapNode(
                        node_id=node_id,
                        node_type=kind,
                        label=skill,
                        status="done" if kind == "skill" else "not_ready",
                        reason="画像中已有" if kind == "skill" else "赛前需补齐",
                    )
                )
                seen.add(node_id)
        for index, material in enumerate(comp.required_materials[:4]):
            nodes.append(
                RoadmapNode(
                    node_id=f"material:{comp.competition_id}:{index}",
                    node_type="material",
                    label=material,
                    status="not_ready",
                    competition_id=comp.competition_id,
                    due_date=comp.submission_deadline or comp.registration_deadline,
                )
            )
        for key, label, due in (
            ("register", "报名", comp.registration_deadline),
            ("submit", "提交", comp.submission_deadline),
            ("defense", "答辩", comp.competition_start_date or comp.competition_end_date),
        ):
            status = "blocked" if due and due < current else "not_ready"
            nodes.append(
                RoadmapNode(
                    node_id=f"milestone:{comp.competition_id}:{key}",
                    node_type="milestone",
                    label=f"{comp.competition_name} · {label}",
                    status=status,
                    competition_id=comp.competition_id,
                    due_date=due,
                    reason="里程碑日期已过" if status == "blocked" else None,
                )
            )
    return nodes


def _roadmap_edges(user: UserProfile, competitions: list[Competition]) -> list[RoadmapEdge]:
    edges: list[RoadmapEdge] = []
    for comp in competitions:
        comp_node = f"competition:{comp.competition_id}"
        for skill in comp.required_skills[:5]:
            kind = "skill" if skill in user.skills else "gap"
            edges.append(RoadmapEdge(source=f"{kind}:{skill}", target=comp_node, relation="enables"))
        previous = comp_node
        for key in ("register", "submit", "defense"):
            target = f"milestone:{comp.competition_id}:{key}"
            edges.append(RoadmapEdge(source=previous, target=target, relation="precedes"))
            previous = target
        for index, _ in enumerate(comp.required_materials[:4]):
            edges.append(
                RoadmapEdge(
                    source=f"material:{comp.competition_id}:{index}",
                    target=f"milestone:{comp.competition_id}:submit",
                    relation="requires",
                )
            )
    return edges


def optimize_portfolios(
    user: UserProfile,
    competitions: list[Competition],
    projects: list[UserProject],
    preferences: PortfolioPreferences,
    current: date | datetime | None = None,
) -> PortfolioOptimizeResponse:
    moment = current or contest_now()
    current = calendar_date(moment)
    comp_map = {comp.competition_id: comp for comp in competitions}
    base_load, existing_ids = _existing_load(
        user, projects, comp_map, current, preferences.horizon_weeks
    )
    recommendations = recommend_for_user(user, competitions, moment)
    rec_map = {
        item.competition_id: item
        for item in recommendations
        # Only source-ready, personally eligible and still-open events enter plans.
        if item.eligible and item.score is not None
    }
    candidates: list[dict] = []
    for comp in competitions:
        rec = rec_map.get(comp.competition_id)
        if rec is None or comp.competition_id in existing_ids:
            continue
        total_hours = _personal_effort(user, comp)
        gain = _skill_gain(user, comp)
        candidates.append(
            {
                "competition": comp,
                "match_score": float(rec.score),
                "total_hours": total_hours,
                "weekly_load": _weekly_curve(total_hours, comp, current, preferences.horizon_weeks),
                "gain": gain,
                "value": _individual_value(preferences.goal, float(rec.score), gain),
            }
        )
    # 组合枚举严格限定前20个正式候选和最多4场，保证响应稳定且结果可复现。
    candidates.sort(key=lambda item: item["value"], reverse=True)
    candidates = candidates[:20]
    plans = [
        _build_mode_plan(mode, user, candidates, base_load, preferences)
        for mode in ("steady", "balanced", "sprint")
    ]
    return PortfolioOptimizeResponse(
        generated_at=datetime.now(timezone.utc).isoformat(),
        plans=plans,
    )
