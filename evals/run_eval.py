"""Offline generated-template regression, not an independently human-labelled set."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ / "src"))
EVALUATION_MOMENT = datetime.fromisoformat("2026-10-03T10:00:00+08:00")
CHECKS = ("intent_ok", "resolve_ok", "citation_ok", "provenance_ok", "refusal_ok",
          "gate_ok", "score_safety_ok", "recommendation_safety_ok", "team_safety_ok",
          "answer_nonempty", "no_error")


def load_competitions(root=PROJ):
    from schemas import Competition

    files = sorted((root / "data/ground_truth/samples").glob("*.json"))
    if not files:
        raise ValueError("No Ground Truth files; evaluation cannot fall back to production data")
    comps = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("competition_id", path.stem)
        for key in ("required_materials", "evaluation_dimensions", "required_skills", "award_distribution"):
            if raw.get(key) is None:
                raw[key] = []
        raw["doc_version"] = raw.get("doc_version") or f"{raw['document_year']}_v1"
        for citation in raw.get("evidence", []):
            citation["trusted_level"] = citation.get("trusted_level") or "C"
            citation["text_exact"] = citation.get("text_exact") or citation["source_text"]
            if citation.get("anchor_confidence") is None:
                citation["anchor_confidence"] = 1.0 if citation.get("anchor_quality") == "exact" else 0.45
        comps.append(Competition.model_validate(raw))
    if len({comp.competition_id for comp in comps}) != len(comps):
        raise ValueError("Duplicate competition IDs in Ground Truth")
    return comps


def build_cases(comps):
    cases = []
    for comp in comps:
        peers = [other for other in comps if other.competition_name == comp.competition_name
                 and other.document_year == comp.document_year]
        ambiguous = len(peers) > 1
        prefix = f"{comp.document_year}年{comp.competition_name}"
        for group, suffix, intent, fields in (
            ("detail", "介绍一下", "detail", ()),
            ("qa_team", "团队几人", "qa", ("team_min", "team_max")),
            ("qa_deadline", "报名什么时候截止", "qa", ("registration_deadline",)),
            ("team", "帮我写一份组队招募文案", "team", ()),
        ):
            cases.append({"case_id": f"{comp.competition_id}:{group}", "group": group,
                          "q": f"{prefix} {suffix}", "expect_cid": None if ambiguous else comp.competition_id,
                          "expect_intent": ("team" if group == "team" else "chat") if ambiguous else intent,
                          "expect_clarification": ambiguous, "target_fields": list(fields),
                          "user_id": "mock_user_001"})
    cases += [
        {"case_id": "global:recommend", "group": "recommend", "q": "推荐适合我的比赛",
         "expect_intent": "recommend", "expect_cid": None, "user_id": "mock_user_001"},
        {"case_id": "global:out_of_domain", "group": "out_of_domain", "q": "今天天气怎么样",
         "expect_intent": "chat", "expect_cid": None},
    ]
    return cases


def run_case(case, comps_by_id):
    import db
    from agent.graph import run_agent
    from evaluation_provenance import citation_identity
    from recommendation.engine import data_validity_check, eligibility_gate
    from trust import assess_recommendation_readiness, assess_source_readiness

    response = run_agent(case["q"], user_id=case.get("user_id"), top_k=4)
    citations = response.get("citations") or []
    answer = response.get("answer") or ""
    resolved = response.get("resolved_competition")
    comp = comps_by_id.get(case.get("expect_cid"))
    known_citations = {citation_identity(c.model_dump(mode="json"))
                       for item in comps_by_id.values() for c in item.evidence}
    if comp is not None:
        known_citations = {citation_identity(c.model_dump(mode="json")) for c in comp.evidence}
    result = {"case_id": case["case_id"], "group": case["group"], "q": case["q"],
              "expected_competition": case.get("expect_cid"), "resolved_competition": resolved,
              "intent": response.get("intent"), "answer": answer, "citations": citations,
              "error": response.get("error"), "assertions": {
                  "intent_ok": response.get("intent") == case["expect_intent"],
                  "resolve_ok": resolved == case.get("expect_cid"),
                  "provenance_ok": all(citation_identity(c) in known_citations for c in citations),
                  "answer_nonempty": bool(answer.strip()),
                  "no_error": not response.get("error"),
              }}
    checks = result["assertions"]
    if case.get("expect_clarification"):
        checks["refusal_ok"] = resolved is None and "请" in answer and any(t in answer for t in ("版本", "赛道"))
        checks["provenance_ok"] = not citations
    if comp:
        fields = set(case["target_fields"])
        supported = [c for c in comp.evidence if c.field in fields and c.source_text.strip()
                     and c.anchor_status not in ("invalid", "needs_review")]
        if fields:
            if supported:
                checks["citation_ok"] = bool(fields & {c.get("field") for c in citations})
            else:
                checks["refusal_ok"] = not citations and any(t in answer for t in ("缺少", "未在", "不能确认", "未能确认"))
        profile = db.get_user_profile(case["user_id"])
        expected = not data_validity_check(comp, EVALUATION_MOMENT) and eligibility_gate(profile, comp, EVALUATION_MOMENT)[0]
        if case["group"] != "team":
            checks["gate_ok"] = response.get("gate", {}).get("eligible") == expected
        score = response.get("score") or {}
        checks["score_safety_ok"] = expected or score.get("total") is None
        if not assess_source_readiness(comp).ready:
            checks["score_safety_ok"] &= "候选" in answer and "你符合报名硬性条件" not in answer
        if case["group"] == "team":
            ready = assess_recommendation_readiness(comp, EVALUATION_MOMENT).ready
            checks["team_safety_ok"] = ("· 报名截止" in answer) if ready else ("暂不生成" in answer and "正在为" not in answer)
    if case["group"] == "recommend":
        recs = response.get("recommendations") or []
        profile = db.get_user_profile(case["user_id"])
        checks["recommendation_safety_ok"] = all(
            r.get("score") is not None and r.get("competition_id") in comps_by_id
            and assess_recommendation_readiness(comps_by_id[r["competition_id"]], EVALUATION_MOMENT).ready
            and eligibility_gate(profile, comps_by_id[r["competition_id"]], EVALUATION_MOMENT)[0]
            for r in recs)
        if not recs:
            checks["refusal_ok"] = "没有" in answer and "赛事大厅" in answer
    result["passed"] = all(checks.values())
    return result


def aggregate(results):
    metrics = {"total_cases": len(results), "passed": sum(r["passed"] for r in results), "checks": {}}
    for key in CHECKS:
        values = [r["assertions"][key] for r in results if key in r["assertions"]]
        metrics["checks"][key] = {"passed": sum(values), "total": len(values),
                                  "rate": round(sum(values) / len(values) * 100, 3) if values else None}
    for alias, key in (("intent_acc", "intent_ok"), ("resolve_acc", "resolve_ok"),
                       ("citation_recall", "citation_ok"), ("gate_consistency", "gate_ok")):
        metrics[alias] = metrics["checks"][key]["rate"]
    return metrics


def render_report(payload):
    lines = ["# 全量模板问答回归报告", "",
             "本报告由赛事名称与四种固定问句自动生成，不是人工标注金标集，也不是独立真实用户评测。",
             "不衡量开放语言模型的泛化能力；门控一致率只比较同源实现，不等同于现实资格判断准确率。", "",
             f"冻结评测时刻：{payload['fixture_moment']}；完成时间：{payload['evaluated_at']}",
             f"结果：**{payload['passed']}/{payload['total_cases']}**；所有异常均计入失败。", "",
             "| 检查 | 通过 / 适用 | 比率 |", "| --- | --- | --- |"]
    for key, item in payload["checks"].items():
        rate = f"{item['rate']:.3f}%" if item["rate"] is not None else "不适用"
        lines.append(f"| {key} | {item['passed']}/{item['total']} | {rate} |")
    lines += ["", "引用召回只统计已有目标字段原文的用例；缺证据时单独检查安全拒答。",
              "推荐允许为空，但任何推荐必须通过来源、日期与画像门控且有分数。",
              "原文一致性比较来源链接、文本、页码、检查日期、锚点及文档指纹，不接受结构化摘要伪造引用。", "",
              "## 可复现性", "",
              f"- 数据文件 SHA-256：`{payload['dataset_sha256']}`",
              f"- 源码 SHA-256：`{payload['code_sha256']}`",
              f"- 赛事快照 SHA-256：`{payload['competition_snapshot_sha256']}`",
              "- 临时 SQLite；LLM、联网搜索与语义模型下载关闭；不读取或写入生产数据库。", "",
              "## 失败明细", ""]
    failed = [r for r in payload["results"] if not r["passed"]]
    if not failed:
        lines.append("无；本轮模板检查全部通过，不能据此推断未覆盖场景也全部正确。")
    for result in failed:
        bad = ", ".join(k for k, value in result["assertions"].items() if not value)
        lines.append(f"- `{result['case_id']}`：{bad}；{result.get('error') or result['q']}")
    return "\n".join(lines) + "\n"


def main():
    with TemporaryDirectory(prefix="campus-template-eval-") as workspace:
        os.environ.update({"DATABASE_URL": "sqlite:///" + workspace.replace("\\", "/") + "/eval.db",
                           "RAG_USE_ST": "0", "AGENT_LLM": "0", "AGENT_LLM_API_KEY": "",
                           "WEB_SEARCH_PROVIDER": "none", "WEB_SEARCH_API_KEY": "", "CHROMA_HOST": ""})
        import db
        import agent.graph as graph
        from evaluation_provenance import code_sha256, competition_snapshot_sha256, dataset_sha256

        comps = load_competitions()
        db.init_db()
        snapshot = db.list_competitions()
        if competition_snapshot_sha256(comps) != competition_snapshot_sha256(snapshot):
            db.get_engine().dispose()
            raise ValueError("Ground Truth and database snapshots differ; do not evaluate stale data")
        by_id = {c.competition_id: c for c in snapshot}
        results = []
        try:
            with patch.object(graph, "contest_now", return_value=EVALUATION_MOMENT):
                for case in build_cases(comps):
                    try:
                        result = run_case(case, by_id)
                    except Exception as exc:
                        result = {"case_id": case["case_id"], "group": case["group"], "q": case["q"],
                                  "assertions": {"no_error": False}, "passed": False,
                                  "error": f"{type(exc).__name__}: {exc}"}
                    results.append(result)
                    if not result["passed"]:
                        print("FAIL", case["case_id"], [k for k, v in result["assertions"].items() if not v])
            payload = {**aggregate(results), "evaluation_type": "generated_template_regression",
                       "fixture_moment": EVALUATION_MOMENT.isoformat(),
                       "evaluated_at": datetime.now(EVALUATION_MOMENT.tzinfo).isoformat(),
                       "dataset_sha256": dataset_sha256(PROJ), "code_sha256": code_sha256(PROJ),
                       "competition_snapshot_sha256": competition_snapshot_sha256(snapshot), "results": results}
            (PROJ / "evals/metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            (PROJ / "evals/report.md").write_text(render_report(payload), encoding="utf-8")
            print(f"Templates: {payload['passed']}/{payload['total_cases']}")
            return 0 if payload["passed"] == payload["total_cases"] else 1
        finally:
            db.get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(main())
