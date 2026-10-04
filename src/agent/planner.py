"""Bounded goal → tool → observation loop. Tools read facts; users apply plans explicitly."""
from __future__ import annotations

import json
import re
from time import perf_counter
from uuid import uuid4

import cn2an
from json_repair import loads as repair_json_loads

import db
from agent import llm
from contest_clock import contest_now
from fact_formatting import format_deadline
from portfolio.optimizer import optimize_portfolios
from recommendation.engine import eligibility_gate, soft_match_score
from schemas import PortfolioPreferences
from trust import assess_source_readiness, is_registerable_now

TOOL_LABELS = {
    "catalog_search": "寻找符合目标的赛事",
    "verify_evidence": "核对官方证据与报名状态",
    "check_eligibility": "核对当轮资格与人数",
    "compare_opportunities": "比较投入、能力缺口与取舍",
    "optimize_portfolio": "按周容量重新规划赛事组合",
    "build_checklist": "生成带依据的执行清单",
}


_NUMBER_TOKEN = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百点]+)"


def _number_value(raw: str | None) -> int | float | None:
    """Parse a scoped Arabic/Chinese number without rewriting the whole question."""
    if not raw:
        return None
    try:
        value = float(raw) if re.fullmatch(r"\d+(?:\.\d+)?", raw) else float(cn2an.cn2an(raw, "smart"))
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def _constraints(question: str) -> dict:
    hours = re.search(
        rf"(?:每周|一周|每星期)\s*(?:只有|只能|能投入|可投入|投入|有|最多|至多|不超过|上限(?:是|为)?)?\s*({_NUMBER_TOKEN})\s*(?:个)?\s*(?:小时|h)",
        question,
        re.I,
    )
    team = re.search(
        rf"(?:我们|团队|队伍|组队|改成|人数)\s*(?:有|是|为|共)?\s*({_NUMBER_TOKEN})\s*(?:个)?人",
        question,
    )
    count = re.search(
        rf"(?:最多|至多|不超过|只(?:选|参加|安排)?|选|参加|安排|规划)\s*({_NUMBER_TOKEN})\s*(?:场|个)",
        question,
    )
    category = next((value for words, value in [
        (("数学建模", "数模"), "modeling"), (("程序设计", "算法竞赛", "编程"), "programming"),
        (("数字媒体", "设计类", "Office", "办公软件"), "design"), (("能源", "机器人"), "robotics_ai"),
    ] if any(word.lower() in question.lower() for word in words)), None)
    return {
        "weekly_hours": _number_value(hours.group(1)) if hours else None,
        "team_size": _number_value(team.group(1)) if team else None,
        "max_competitions": _number_value(count.group(1)) if count else 3,
        "category": category,
        "goal": (
            "award" if any(word in question for word in ("获奖", "拿奖", "冲奖", "得奖"))
            else "growth" if any(word in question for word in ("学习", "成长", "练习", "提升"))
            else "balanced"
        ),
    }


def _available(state: dict) -> list[str]:
    if "candidates" not in state:
        return ["catalog_search"]
    if "verified" not in state:
        return ["verify_evidence"]
    if not state["verified"] or state.get("profile") is None or state.get("invalid_constraints"):
        return []
    if "eligible" not in state:
        return ["check_eligibility"]
    if not state["eligible"]:
        return []
    available = []
    if "comparisons" not in state:
        available.append("compare_opportunities")
    if "plans" not in state:
        available.append("optimize_portfolio")
    if "plans" in state and "checklist" not in state:
        available.append("build_checklist")
    return available


def choose_tool(question: str, allowed: list[str], observations: list[dict], model=None) -> tuple[str, str]:
    """The model selects a tool from the current allowlist, never supplies executable code or facts."""
    if len(allowed) == 1 or not llm.is_llm_enabled():
        return allowed[0], "offline_policy"
    # Send only public aggregate observations and a redacted goal, never the student's profile.
    # The raw user question may contain personal information; do not send it to the planner.
    objective = "比较不同机会的取舍" if any(word in question for word in ("比较", "对比", "取舍")) else "选择可执行的参赛路线"
    response = llm._post_chat(
        "你是校园参赛任务规划器。依据已执行工具的公开结果选择下一步工具。"
        "仅返回JSON对象 {\"tool\":\"工具名\"}，必须从available_tools选择，不输出推理过程、事实或代码。",
        json.dumps({"goal": objective, "available_tools": allowed, "observations": observations}, ensure_ascii=False),
        model=model, timeout=6,
    )
    selector = "model"
    try:
        action = json.loads(response or "{}")
    except (ValueError, TypeError):
        try:
            action = repair_json_loads(response or "{}")
            selector = "model_repaired"
        except (ValueError, TypeError):
            action = {}
    try:
        if isinstance(action, dict) and set(action) == {"tool"} and action["tool"] in allowed:
            return action["tool"], selector
    except (TypeError, KeyError):
        pass
    return allowed[0], "policy_fallback"


def _execute(tool: str, state: dict) -> dict:
    competitions = state["catalog"]
    constraints = state["constraints"]
    if tool == "catalog_search":
        target = state.get("competition_id")
        category = constraints["category"]
        named = [c.competition_id for c in competitions if c.competition_id in state["question"]
                 or c.competition_name in state["question"]]
        aliases = {"智能体互联": "aic_interconnect_2026", "AI+能源": "aic_energy_2026", "AI+6G": "aic_6g_2026",
                   "Office高级应用": "ncccu_office_2026", "程序设计挑战赛": "ncccu_program_2026"}
        named.extend(cid for alias, cid in aliases.items() if alias.lower() in state["question"].lower())
        if not target and not named and "杯" in state["question"]:
            from agent.graph import _resolve_competition_versioned
            resolved, _, clarification = _resolve_competition_versioned(state["question"], competitions)
            if resolved:
                named = [resolved.competition_id]
            else:
                state["clarification"] = clarification or "请提供该赛事的正式名称和年份。"
        candidates = [c for c in competitions if not state.get("clarification")
                      and (not target or c.competition_id == target) and (not named or c.competition_id in named)
                      and (not category or c.category.value == category)]
        state["candidates"] = candidates
        return {"matched": len(candidates), "category": category, "explicit_target": bool(target)}
    if tool == "verify_evidence":
        verified = []
        rejected = []
        for comp in state["candidates"]:
            assessment = assess_source_readiness(comp)
            open_now = is_registerable_now(comp, state["now"])
            if assessment.ready and open_now:
                verified.append(comp)
            else:
                rejected.append({"competition_id": comp.competition_id, "competition_name": comp.competition_name,
                                 "reasons": list(assessment.reasons) + ([] if open_now else ["当前已不可报名"])})
        state["verified"], state["rejected"] = verified, rejected
        return {"ready_and_open": len(verified), "rejected": len(rejected)}
    if tool == "check_eligibility":
        eligible = []
        gates = {}
        for comp in state["verified"]:
            eligible_now, reasons = eligibility_gate(state["profile"], comp, state["now"])
            gates[comp.competition_id] = {"eligible": eligible_now, "reasons": [r.reason for r in reasons]}
            if eligible_now:
                eligible.append(comp)
            else:
                state["rejected"].append({"competition_id": comp.competition_id, "competition_name": comp.competition_name,
                                           "reasons": [r.reason for r in reasons]})
        state["eligible"], state["gates"] = eligible, gates
        return {"eligible": len(eligible), "blocked": len(state["verified"]) - len(eligible)}
    if tool == "compare_opportunities":
        comparisons = []
        for comp in state["eligible"]:
            score = soft_match_score(state["profile"], comp, state["now"])
            comparisons.append({"competition_id": comp.competition_id, "name": comp.competition_name,
                                "score": score.total, "breakdown": score.model_dump(mode="json"),
                                "skill_gaps": [s for s in comp.required_skills if s not in state["profile"].skills]})
        comparisons.sort(key=lambda item: item["score"], reverse=True)
        state["comparisons"] = comparisons
        return {"compared": len(comparisons), "top_ids": [item["competition_id"] for item in comparisons[:3]]}
    if tool == "optimize_portfolio":
        preferences = PortfolioPreferences(max_competitions=constraints["max_competitions"], goal=constraints["goal"])
        # Keep all existing-project competitions, including those outside the requested category,
        # so their remaining work continues to consume the student's weekly capacity.
        included = {c.competition_id for c in state["eligible"]} | {p.competition_id for p in state["projects"]}
        planning_catalog = [c for c in state["catalog"] if c.competition_id in included]
        result = optimize_portfolios(state["profile"], planning_catalog, state["projects"], preferences, state["now"])
        state["plans"] = result.model_dump(mode="json")["plans"]
        return {"feasible_modes": sum(bool(p["items"]) for p in state["plans"]),
                "selected_counts": [len(p["items"]) for p in state["plans"]]}
    if tool == "build_checklist":
        selected = next((p for p in state["plans"] if p["mode"] == "balanced" and p["items"]),
                        next((p for p in state["plans"] if p["items"]), None))
        comp_map = {c.competition_id: c for c in state["eligible"]}
        state["checklist"] = []
        for item in selected["items"] if selected else []:
            comp = comp_map[item["competition_id"]]
            state["checklist"].append({"competition_id": comp.competition_id,
                                       "actions": ["打开官方通知，核对学校名额、组队与报名要求", "确认选题和成员分工",
                                                   "按官方材料清单安排制作和提交", "提交前再次检查官网更新"],
                                       "required_materials": comp.required_materials,
                                       "registration_deadline": format_deadline(comp, "registration_deadline")})
        return {"checklists": len(state["checklist"]), "automatic_writes": 0}
    raise ValueError("Unknown tool")


def run_planning_task(question: str, user_id=None, competition_id=None, model=None) -> dict:
    started = perf_counter()
    constraints = _constraints(question)
    saved_profile = db.get_user_profile(user_id) if user_id else None
    from agent.graph import _load_profile
    turn_profile = _load_profile({"user_id": user_id, "question": question}) if user_id else None
    profile = turn_profile.model_copy(deep=True) if turn_profile else None
    invalid = []
    if constraints["weekly_hours"] is not None:
        if not isinstance(constraints["weekly_hours"], int):
            invalid.append("每周可投入时间请使用整数小时")
        elif not 0 <= constraints["weekly_hours"] <= 168:
            invalid.append("每周可投入时间须在0—168小时之间")
        elif profile:
            profile.weekly_available_hours = constraints["weekly_hours"]
    if constraints["team_size"] is not None:
        if not isinstance(constraints["team_size"], int) or not 1 <= constraints["team_size"] <= 10:
            invalid.append("请提供1—10人的实际团队人数")
        elif profile:
            profile.expected_team_size = constraints["team_size"]
    if not isinstance(constraints["max_competitions"], int) or not 1 <= constraints["max_competitions"] <= 4:
        invalid.append("最多参赛数量须在1—4场之间")
    state = dict(catalog=db.get_all_competitions(), constraints=constraints, profile=profile, question=question,
                 projects=db.list_user_projects(user_id) if profile else [], now=contest_now(),
                 competition_id=competition_id, invalid_constraints=invalid)
    data_version = "planning:" + str(len(state["catalog"])) + "@" + max(
        (str(c.last_verified_at or "unverified") for c in state["catalog"]), default="empty")
    trace, observations, decisions = [], [], []
    for _ in range(6):
        allowed = _available(state)
        if not allowed:
            break
        tool, selector = choose_tool(question, allowed, observations, model=model)
        step_start = perf_counter()
        try:
            observation = _execute(tool, state)
            status = "succeeded"
        except Exception as exc:
            # A failed evidence/gate/tool must not leave partially validated recommendations.
            observation = {"error": exc.__class__.__name__, "requires_retry": True}
            status = "failed"
            state["tool_failure"] = tool
        observations.append({"tool": tool, "result": observation})
        decisions.append({"tool": tool, "selector": selector, "available_tools": allowed, "observation": observation})
        trace.append({"node": tool, "label": TOOL_LABELS[tool], "status": status,
                      "duration_ms": round((perf_counter() - step_start) * 1000, 3),
                      "evidence_count": sum(len(c.evidence) for c in state.get("verified", [])),
                      "data_version": data_version, "detail": json.dumps(observation, ensure_ascii=False),
                      "fallback": selector == "policy_fallback" or status == "failed"})
        if status == "failed":
            break
    completed = "checklist" in state and bool(state["checklist"]) and not state.get("tool_failure")
    lines = ["已按你的目标核对赛事、资格与时间容量。"]
    citations, recommendations = [], []
    if invalid:
        lines.extend(invalid)
    elif state.get("tool_failure"):
        lines.append("本轮工具执行未完成，暂不提供可采用的参赛方案；请重试。")
    elif not state.get("candidates"):
        lines.append(state.get("clarification") or "没有找到符合目标的赛事。请明确赛事名称或调整类别；不会换成其他赛事作答。")
    elif not state.get("verified"):
        lines.append("当前目标中没有同时满足完整官方证据且仍可报名的赛事。请补齐来源或选择其他目标。")
    elif profile is None:
        lines.append("请登录并开启“参考我的画像”，补充学籍、团队人数和每周时间后再规划。")
    elif not state.get("eligible"):
        lines.append("当前资格或团队人数不符合可报名赛事要求。请核对实际人数与学籍，不自动放宽资格条件。")
    elif not completed:
        lines.append("当前周容量与已有项目约束下没有可行组合。请减少计划赛事数量、调整真实时间预算，或先完成已有项目；不会自动增加你的可用时间。")
    if completed:
        selected = next((p for p in state["plans"] if p["mode"] == "balanced" and p["items"]),
                        next(p for p in state["plans"] if p["items"]))
        lines.append(f"本轮每周预算 {profile.weekly_available_hours} 小时，团队 {profile.expected_team_size} 人；修改仅用于本次规划。")
        lines.append("建议采用" + selected["label"] + "：")
        comp_map = {c.competition_id: c for c in state["eligible"]}
        for item in selected["items"]:
            comp = comp_map[item["competition_id"]]
            cite = next(e for e in comp.evidence if e.field == "registration_deadline")
            citations.append(cite.model_dump(mode="json"))
            lines.append(f"- {comp.competition_name}：报名截止 {format_deadline(comp, 'registration_deadline')} [{len(citations)}]；预计个人投入 {item['estimated_total_hours']} 小时（系统估算）。")
            lines.append("  官方材料：" + "、".join(comp.required_materials))
            recommendations.append({"competition_id": comp.competition_id, "competition_name": comp.competition_name,
                                    "eligible": True, "score": item["match_score"], "reasons": item["reasons"]})
        lines.append(f"峰值周投入 {selected['peak_weekly_load']} 小时；估算工作量未经过用户实际工时校准。")
        lines.append("下一步：查看行动路线核对取舍，再由你采用方案生成项目任务；本次规划未写入项目。")
    rejected = state.get("rejected", [])
    if rejected:
        lines.append(f"另有 {len(rejected)} 项被证据、时间或资格条件排除。")
        if competition_id:
            lines.extend(item["competition_name"] + "：" + "；".join(item["reasons"]) for item in rejected[:2])
    lines.append("报名与最终资格以官网最新通知及学校审核为准。")
    duration = round((perf_counter() - started) * 1000, 3)
    public_state = {"status": "completed" if completed else "needs_input", "constraints": constraints,
                    "decisions": decisions, "excluded": rejected, "checklist": state.get("checklist", []),
                    "plans": state.get("plans", []) if not state.get("tool_failure") else [],
                    "profile_changed": False, "automatic_writes": 0,
                    "uses_temporary_profile": bool(profile and saved_profile and (
                        profile.weekly_available_hours != saved_profile.weekly_available_hours
                        or profile.expected_team_size != saved_profile.expected_team_size
                        or profile.education_level != saved_profile.education_level)),
                    "planner_mode": "model_assisted" if any(d["selector"].startswith("model") for d in decisions) else "offline_policy"}
    return dict(run_id="agent_" + uuid4().hex, question=question, intent="plan", resolved_competition=competition_id,
                resolved_name=next((c.competition_name for c in state["catalog"] if c.competition_id == competition_id), None),
                answer="\n".join(lines), citations=citations, gate={}, score={}, recommendations=recommendations,
                teammate_matches=[], web_results=[], pending_review=not completed, data_version=data_version,
                trace=trace, trace_summary=trace, trace_legacy=[step["detail"] for step in trace],
                metrics={"total_duration_ms": duration, "node_count": len(trace), "evidence_count": len(citations)},
                error=state.get("tool_failure"), planning=public_state)
