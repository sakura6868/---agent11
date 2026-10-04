"""Run the 15 submission metrics cases and the complete regression suite."""

from __future__ import annotations

import hashlib
import atexit
import io
import json
import platform
import os
import re
import subprocess
import sys
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
GT_DIR = PROJECT_ROOT / "data" / "ground_truth" / "samples"
RESULT_PATH = PROJECT_ROOT / "evals" / "formal_results.json"
REPORT_PATH = PROJECT_ROOT / "docs" / "QUANTITATIVE_EVALUATION.md"
sys.path.insert(0, str(SRC_DIR))

EVALUATION_DATE = date(2026, 10, 3)
_workspace = TemporaryDirectory(prefix="campus-agent-formal-")
os.environ["DATABASE_URL"] = "sqlite:///" + _workspace.name.replace("\\", "/") + "/formal.db"
os.environ["RAG_USE_ST"] = "0"
os.environ["AGENT_LLM"] = "0"
os.environ["WEB_SEARCH_PROVIDER"] = "none"
os.environ["WEB_SEARCH_API_KEY"] = ""

import db  # noqa: E402
from agent.graph import run_agent  # noqa: E402
from rag.store import get_rag  # noqa: E402
from recommendation.engine import recommend_for_user  # noqa: E402
from schemas import (  # noqa: E402
    Citation,
    Competition,
    CompetitionCategory,
    DataStatus,
    EducationLevel,
    Grade,
    TrustedLevel,
    UserProfile,
)


from trust import REQUIRED_EVIDENCE_FIELDS, _evidence_is_complete


def _cleanup_workspace():
    db.get_engine().dispose()
    _workspace.cleanup()


atexit.register(_cleanup_workspace)

METRIC_LABELS = {
    "deadline_consistency": "截止日期一致率",
    "eligibility_accuracy": "资格判断准确率",
    "citation_accuracy": "官方证据引用正确率",
    "unverified_interception": "未核验赛事拦截率",
    "insufficient_refusal_rate": "证据不足安全处理率",
}


@dataclass(frozen=True)
class FormalCase:
    case_id: str
    metric: str
    scenario: str
    check: Callable[[], str]


def profile(user_id: str = "formal_eval_user") -> UserProfile:
    return UserProfile(
        user_id=user_id,
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SOPHOMORE,
        major="软件工程",
        skills=["Python"],
        weekly_available_hours=12,
        expected_team_size=3,
        privacy_consent=True,
    )


def synthetic_competition(**overrides) -> Competition:
    checked_at = EVALUATION_DATE.isoformat()
    source_url = "https://example.edu/formal-evaluation"
    evidence = [
        Citation(
            field=field,
            page=None,
            source_text=f"合成测试夹具（非真实官方原文）：{field}",
            document_name="formal-evaluation.html",
            source_url=source_url,
            acquired_date=checked_at,
            last_verified_at=checked_at,
            trusted_level=TrustedLevel.A,
        )
        for field in REQUIRED_EVIDENCE_FIELDS
    ]
    values = {
        "competition_id": "formal_eval_competition",
        "competition_name": "正式评测赛事",
        "document_year": EVALUATION_DATE.year,
        "category": CompetitionCategory.SOFTWARE,
        "eligible_students": [EducationLevel.UNDERGRADUATE],
        "team_min": 1,
        "team_max": 3,
        "required_materials": ["报名表"],
        "registration_deadline": EVALUATION_DATE + timedelta(days=30),
        "official_source_url": source_url,
        "official_source_status": "found",
        "source_acquired_date": checked_at,
        "trusted_level": TrustedLevel.A,
        "data_status": DataStatus.VERIFIED,
        "last_verified_at": checked_at,
        "evidence": evidence,
    }
    values.update(overrides)
    return Competition(**values)


def ground_truth_rows() -> list[tuple[Path, dict, Competition]]:
    rows = []
    for path in sorted(GT_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        comp = db.get_competition(path.stem)
        if comp is None:
            raise AssertionError(f"数据库缺少 {path.stem}")
        rows.append((path, raw, comp))
    if len(rows) < 21:
        raise AssertionError(f"预期至少21份 Ground Truth，实际{len(rows)}份")
    return rows


def deadline_registration_matches() -> str:
    rows = ground_truth_rows()
    for path, raw, comp in rows:
        actual = comp.registration_deadline.isoformat() if comp.registration_deadline else None
        if actual != raw.get("registration_deadline"):
            raise AssertionError(f"{path.name}: {actual} != {raw.get('registration_deadline')}")
        expected_at = datetime.fromisoformat(raw["registration_deadline_at"]) if raw.get("registration_deadline_at") else None
        if comp.registration_deadline_at != expected_at:
            raise AssertionError(f"{path.name}: 精确报名时刻不一致")
    return f"{len(rows)}/{len(rows)} 项报名截止日期与 Ground Truth 一致"


def deadline_submission_matches() -> str:
    rows = ground_truth_rows()
    for path, raw, comp in rows:
        actual = comp.submission_deadline.isoformat() if comp.submission_deadline else None
        if actual != raw.get("submission_deadline"):
            raise AssertionError(f"{path.name}: {actual} != {raw.get('submission_deadline')}")
        expected_at = datetime.fromisoformat(raw["submission_deadline_at"]) if raw.get("submission_deadline_at") else None
        if comp.submission_deadline_at != expected_at:
            raise AssertionError(f"{path.name}: 精确提交时刻不一致")
    return f"{len(rows)}/{len(rows)} 项提交截止日期与 Ground Truth 一致"


def agent_uses_unique_canonical_deadline() -> str:
    core_ids = ("china_softcup_2026", "mathorcup_2026", "mcm_cn_2026", "mai_qihang_2026", "mathorcup_data_2026")
    for competition_id in core_ids:
        comp = db.get_competition(competition_id)
        result = run_agent("报名截止日期是什么时候", competition_id=competition_id, top_k=4)
        dates = re.findall(r"报名截止(?:日期|时间)：(\d{4}-\d{2}-\d{2})", result["answer"])
        expected = comp.registration_deadline.isoformat()
        if dates != [expected]:
            raise AssertionError(f"{competition_id}: {dates} != [{expected}]")
        if comp.registration_deadline_at is not None:
            expected_time = comp.registration_deadline_at.strftime("%Y-%m-%d %H:%M")
            if expected_time not in result["answer"] or "校园时区假设" not in result["answer"]:
                raise AssertionError(f"{competition_id}: 精确截止时间或时区假设未披露")
    return f"{len(core_ids)}/{len(core_ids)} 个核心赛事仅输出一个规范报名截止日期，保留官方时刻与时区假设"


def eligible_open_competition_is_scored() -> str:
    result = recommend_for_user(profile(), [synthetic_competition()], EVALUATION_DATE)[0]
    if not result.eligible or result.score is None or result.recommendation_status == "ineligible":
        raise AssertionError("已核验开放赛事未进入正式评分")
    return f"开放且符合资格的赛事进入正式评分，score={result.score}"


def expired_registration_is_rejected() -> str:
    comp = synthetic_competition(registration_deadline=EVALUATION_DATE - timedelta(days=1))
    result = recommend_for_user(profile(), [comp], EVALUATION_DATE)[0]
    if result.eligible or result.score is not None or result.recommendation_status != "ineligible":
        raise AssertionError("报名已截止赛事未被正确拒绝")
    return "报名已截止赛事为 ineligible，score=null"


def expired_submission_fallback_is_rejected() -> str:
    comp = synthetic_competition(
        registration_deadline=None,
        submission_deadline=EVALUATION_DATE - timedelta(days=1),
    )
    result = recommend_for_user(profile(), [comp], EVALUATION_DATE)[0]
    if result.eligible or result.score is not None or result.recommendation_status != "ineligible":
        raise AssertionError("缺少报名日时未按提交截止日期拒绝")
    return "报名日缺失时使用提交截止日判断，score=null"


def load_core_evidence() -> list[tuple[str, dict]]:
    records = []
    for path in sorted(GT_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("data_status") == "verified":
            records.append((path.stem, raw))
    if not records:
        raise AssertionError("没有推荐级证据，不能以空集合宣称引用正确率100%")
    return records


def core_fields_have_evidence() -> str:
    required = REQUIRED_EVIDENCE_FIELDS
    for competition_id, raw in load_core_evidence():
        fields = {item["field"] for item in raw.get("evidence", []) if _evidence_is_complete(Citation.model_validate(item))}
        missing = required - fields
        if missing:
            raise AssertionError(f"{competition_id} 缺少字段证据: {sorted(missing)}")
    count = len(load_core_evidence())
    return f"{count}/{count} 个官网来源已确认赛事的5类关键字段均有完整证据"


def core_evidence_metadata_is_complete() -> str:
    checked = 0
    for competition_id, raw in load_core_evidence():
        for item in raw.get("evidence", []):
            checked += 1
            if not _evidence_is_complete(Citation.model_validate(item)):
                raise AssertionError(f"{competition_id}.{item.get('field')} 未通过统一证据完整性规则")
            is_pdf = str(item.get("document_name", "")).lower().endswith(".pdf") or ".pdf" in str(item.get("source_url", "")).lower()
            if is_pdf and not isinstance(item.get("page"), int):
                raise AssertionError(f"{competition_id}.{item.get('field')} 的 PDF 证据缺少页码")
            if not item.get("source_text") or not str(item.get("source_url", "")).startswith("http"):
                raise AssertionError(f"{competition_id}.{item.get('field')} 原文或链接无效")
            if not item.get("acquired_date") or not item.get("last_verified_at"):
                raise AssertionError(f"{competition_id}.{item.get('field')} 日期元数据不完整")
    return f"{checked}/{checked} 条推荐级证据具备定位信息、原文、链接、获取和来源检查日期"


def rag_deadline_citations_are_official() -> str:
    core_ids = ("china_softcup_2026", "mathorcup_2026", "mcm_cn_2026", "mai_qihang_2026", "mathorcup_data_2026")
    for competition_id in core_ids:
        citations = get_rag().query(competition_id, "报名截止日期是什么时候", top_k=4)
        deadline_hits = [item for item in citations if item.field == "registration_deadline"]
        if not deadline_hits:
            raise AssertionError(f"{competition_id} 未召回报名截止证据")
        if any(not item.source_url.startswith("http") or not item.source_text for item in deadline_hits):
            raise AssertionError(f"{competition_id} 召回证据缺少官方链接或原文")
        originals = {item.source_text for item in db.get_competition(competition_id).evidence}
        if any(item.source_text not in originals or not item.citation_id for item in deadline_hits):
            raise AssertionError(f"{competition_id} 召回了非原始证据")
    return f"{len(core_ids)}/{len(core_ids)} 个核心赛事的截止检索命中保存的原文证据、证据ID和官方链接"


def unverified_data_cannot_be_recommended() -> str:
    comp = synthetic_competition(
        data_status=DataStatus.UNVERIFIED,
        trusted_level=TrustedLevel.B,
        last_verified_at=None,
    )
    result = recommend_for_user(profile(), [comp], EVALUATION_DATE)[0]
    if result.eligible or result.score is not None or result.recommendation_status != "candidate_only":
        raise AssertionError("未核验赛事错误进入正式推荐")
    return "未核验赛事为 candidate_only，eligible=false，score=null"


def unverified_project_creation_is_blocked() -> str:
    comp = synthetic_competition(
        competition_id="formal_eval_candidate", data_status=DataStatus.UNVERIFIED,
        trusted_level=TrustedLevel.B, registration_deadline=date(2099, 1, 1),
    )
    db.upsert_competition(comp)
    user_id = "formal_eval_block_user"
    db.delete_user_profile(user_id)
    db.save_user_profile(profile(user_id))
    try:
        try:
            db.create_user_project(user_id, comp.competition_id)
        except ValueError as exc:
            if str(exc) != "competition_basic_info_incomplete":
                raise
        else:
            raise AssertionError("未核验赛事错误创建项目")
    finally:
        db.delete_user_profile(user_id)
    return "独立未核验夹具不能创建新项目"


def unverified_agent_marks_candidate() -> str:
    comp = synthetic_competition(competition_id="formal_eval_agent_candidate", data_status=DataStatus.UNVERIFIED, trusted_level=TrustedLevel.B)
    db.upsert_competition(comp)
    result = run_agent("报名截止日期是什么时候", competition_id=comp.competition_id, top_k=4)
    if not result["pending_review"] or "候选信息" not in result["answer"]:
        raise AssertionError("Agent 未明确标注候选边界")
    return "Agent 明确说明候选信息不构成资格判断或推荐评分"


def missing_national_deadline_is_not_invented() -> str:
    comp = synthetic_competition(competition_id="formal_missing_deadline", registration_deadline=None,
                                 evidence=[], data_status=DataStatus.UNVERIFIED, trusted_level=TrustedLevel.B)
    db.upsert_competition(comp)
    result = run_agent("报名截止日期是什么时候", competition_id=comp.competition_id, top_k=4)
    if "不能确认截止时间" not in result["answer"] or result["citations"]:
        raise AssertionError("缺少证据时未拒答或伪造了引用")
    if "官方文件未给出" in result["answer"] or re.search(r"\d{4}-\d{2}-\d{2}", result["answer"]):
        raise AssertionError("Agent 编造了报名截止日期")
    return "独立缺证据夹具：不能确认日期，不伪造原文或断言官网没有日期"


def ambiguous_versions_declare_latest_official_source() -> str:
    comps = [synthetic_competition(competition_id=f"formal_version_{year}", competition_name="正式版本测试赛事", document_year=year) for year in (2025, 2026)]
    for comp in comps:
        db.upsert_competition(comp)
    with patch.object(db, "get_all_competitions", return_value=comps):
        result = run_agent("正式版本测试赛事报名截止")
    if result["resolved_competition"] != "formal_version_2026":
        raise AssertionError("同名多年份赛事未选中最新官方来源版本")
    if "你没有指定年份" not in result["answer"] or "2026年" not in result["answer"]:
        raise AssertionError("默认版本未在答案中显式声明")
    return "同名多年份赛事显式声明默认版本"


def out_of_domain_question_is_refused() -> str:
    result = run_agent("今天天气怎么样")
    if result["intent"] != "chat" or result["resolved_competition"] is not None or result["citations"]:
        raise AssertionError("域外问题被错误路由或引用赛事证据")
    if "请告诉我具体赛事名称" not in result["answer"]:
        raise AssertionError("域外问题未返回能力边界说明")
    return "域外问题不生成赛事事实，提示补充具体赛事名称"


def build_cases() -> list[FormalCase]:
    return [
        FormalCase("FE-01", "deadline_consistency", "报名日期与 Ground Truth 一致", deadline_registration_matches),
        FormalCase("FE-02", "deadline_consistency", "提交日期与 Ground Truth 一致", deadline_submission_matches),
        FormalCase("FE-03", "deadline_consistency", "Agent 仅输出规范截止日期", agent_uses_unique_canonical_deadline),
        FormalCase("FE-04", "eligibility_accuracy", "开放且符合资格赛事进入评分", eligible_open_competition_is_scored),
        FormalCase("FE-05", "eligibility_accuracy", "报名已截止赛事不评分", expired_registration_is_rejected),
        FormalCase("FE-06", "eligibility_accuracy", "报名日缺失时检查提交日", expired_submission_fallback_is_rejected),
        FormalCase("FE-07", "citation_accuracy", "核心字段均有证据", core_fields_have_evidence),
        FormalCase("FE-08", "citation_accuracy", "证据元数据完整", core_evidence_metadata_is_complete),
        FormalCase("FE-09", "citation_accuracy", "RAG 命中官方截止证据", rag_deadline_citations_are_official),
        FormalCase("FE-10", "unverified_interception", "未核验赛事不评分", unverified_data_cannot_be_recommended),
        FormalCase("FE-11", "unverified_interception", "未核验赛事不能新建项目", unverified_project_creation_is_blocked),
        FormalCase("FE-12", "unverified_interception", "Agent 明确标注候选", unverified_agent_marks_candidate),
        FormalCase("FE-13", "insufficient_refusal_rate", "官方未给日期时不编造", missing_national_deadline_is_not_invented),
        FormalCase("FE-14", "insufficient_refusal_rate", "同名多年份显式声明默认版本", ambiguous_versions_declare_latest_official_source),
        FormalCase("FE-15", "insufficient_refusal_rate", "域外问题拒绝生成赛事事实", out_of_domain_question_is_refused),
    ]


def run_regression_suite() -> dict:
    """跑完整回归套件。

    原来用 unittest.discover 只能发现 52 项（漏掉 pytest 风格用例），
    与文档口径「59 项自动回归」不一致；改用 pytest 既覆盖更全又与对外口径一致。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    tail = lines[-1] if lines else ""

    def _num(pattern: str) -> int:
        matched = re.search(pattern, tail)
        return int(matched.group(1)) if matched else 0

    passed = _num(r"(\d+) passed")
    failures = _num(r"(\d+) failed")
    errors = _num(r"(\d+) error")
    subtests = _num(r"(\d+) subtests passed")
    return {
        "tests_run": passed + failures + errors,
        "passed": passed,
        "failures": failures,
        "errors": errors,
        "subtests_passed": subtests,
        "successful": proc.returncode == 0,
        "output": tail,
    }


def latest_freeze() -> dict | None:
    archives = sorted((PROJECT_ROOT / "backups").glob("campus-agent-freeze-*.zip"))
    if not archives:
        return None
    path = archives[-1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": path.relative_to(PROJECT_ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": digest}


def render_report(payload: dict) -> str:
    all_formal_passed = payload["formal_summary"]["passed"] == payload["formal_summary"]["total"]
    all_regression_passed = payload["regression"]["successful"]
    if all_formal_passed and all_regression_passed:
        conclusion = "本轮15条正式指标用例全部通过，五项核心指标均为100%。"
    else:
        conclusion = (
            f"本轮正式指标通过 {payload['formal_summary']['passed']}/{payload['formal_summary']['total']}，"
            f"自动回归通过 {payload['regression']['passed']}/{payload['regression']['tests_run']}；"
            "未通过项须修复后才能作为提交版结果。"
        )
    lines = [
        "# 校园科创导航智能体量化评测报告",
        "",
        f"- 评测时间：{payload['evaluated_at']}",
        f"- 运行环境：Python {payload['environment']['python']} / {payload['environment']['platform']}",
        f"- 正式指标用例：{payload['formal_summary']['passed']}/{payload['formal_summary']['total']} 通过",
        f"- 自动回归测试：{payload['regression']['passed']}/{payload['regression']['tests_run']} 通过",
        "",
        "## 核心指标",
        "",
        "| 指标 | 通过/总数 | 结果 |",
        "| --- | ---: | ---: |",
    ]
    for metric, label in METRIC_LABELS.items():
        item = payload["metrics"][metric]
        lines.append(f"| {label} | {item['passed']}/{item['total']} | **{item['rate']:.1f}%** |")

    lines.extend(["", "## 15条正式用例", "", "| ID | 指标 | 场景 | 结果 | 实测证据 |", "| --- | --- | --- | --- | --- |"])
    for item in payload["cases"]:
        status = "通过" if item["passed"] else "失败"
        detail = str(item["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['case_id']} | {METRIC_LABELS[item['metric']]} | {item['scenario']} | {status} | {detail} |")

    lines.extend([
        "",
        f"## {payload['regression']['tests_run']}项自动测试",
        "",
        "执行命令：",
        "",
        "```powershell",
        ".\\venv\\Scripts\\python.exe -m pytest tests -q",
        "```",
        "",
        f"本次实际运行 {payload['regression']['tests_run']} 项，通过 {payload['regression']['passed']} 项，"
        f"失败 {payload['regression']['failures']} 项，错误 {payload['regression']['errors']} 项。",
        "测试范围包括 Ground Truth、推荐门控、学历校验、证据完整性、RAG/Agent 日期一致性、项目工作台、ICS、删除和多年份消歧。",
        "",
        "## 结论与边界",
        "",
        conclusion + "该结果仅适用于当前冻结数据和测试范围，"
        "不表示所有未来赛事或任意自然语言输入均能达到100%；新增赛事必须经过相同的证据核验和回归流程。",
    ])
    if payload.get("freeze"):
        freeze = payload["freeze"]
        lines.extend([
            "",
            "## 冻结版本",
            "",
            f"- 备份包：`{freeze['path']}`",
            f"- 文件大小：{freeze['bytes']} bytes",
            f"- SHA-256：`{freeze['sha256']}`",
        ])
    lines.extend(["", "机器可读结果：`evals/formal_results.json`", ""])
    return "\n".join(lines)


def main() -> int:
    db.init_db()
    from evaluation_provenance import code_sha256, competition_snapshot_sha256, dataset_sha256

    initial_snapshot_sha256 = competition_snapshot_sha256(db.list_competitions())
    results = []
    for case in build_cases():
        try:
            detail = case.check()
            passed = True
        except Exception as exc:  # keep the complete report even when one case fails
            detail = f"{type(exc).__name__}: {exc}"
            passed = False
        results.append(
            {
                "case_id": case.case_id,
                "metric": case.metric,
                "scenario": case.scenario,
                "passed": passed,
                "detail": detail,
            }
        )
        print(f"[{case.case_id}] {'PASS' if passed else 'FAIL'} {case.scenario}: {detail}")

    metrics = {}
    for metric in METRIC_LABELS:
        items = [item for item in results if item["metric"] == metric]
        passed = sum(1 for item in items if item["passed"])
        metrics[metric] = {"passed": passed, "total": len(items), "rate": passed / len(items) * 100}

    regression = run_regression_suite()
    payload = {
        "evaluated_at": date.today().isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "formal_summary": {
            "passed": sum(1 for item in results if item["passed"]),
            "total": len(results),
        },
        "metrics": metrics,
        "cases": results,
        "regression": regression,
        "freeze": None,
        "fixture_date": EVALUATION_DATE.isoformat(),
        "dataset_sha256": dataset_sha256(PROJECT_ROOT),
        "code_sha256": code_sha256(PROJECT_ROOT),
        "competition_snapshot_sha256": initial_snapshot_sha256,
    }
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(render_report(payload), encoding="utf-8")

    formal_ok = payload["formal_summary"]["passed"] == payload["formal_summary"]["total"]
    all_ok = formal_ok and regression["successful"] and regression["tests_run"] >= 24
    print(f"Formal: {payload['formal_summary']['passed']}/{payload['formal_summary']['total']}")
    print(f"Regression: {regression['passed']}/{regression['tests_run']}")
    print(f"Report: {REPORT_PATH}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
