from __future__ import annotations
import hashlib
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api
import db
from agent.graph import run_agent
from agent.planner import choose_tool, run_planning_task
from deployment_security import validate_production_configuration
from schemas import UserProfile

NOW = datetime.fromisoformat("2026-10-04T12:00:00+08:00")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def planner_user():
    uid = "planner_" + uuid4().hex[:10]
    db.save_user_profile(UserProfile(user_id=uid, education_level="本科生", grade="大二", major="计算机科学与技术",
                                     skills=["Python", "数据结构"], weekly_available_hours=20, expected_team_size=1,
                                     privacy_consent=True))
    with patch("agent.planner.contest_now", return_value=NOW):
        yield uid
    db.delete_user_profile(uid)


def test_open_track_snapshots_match_fingerprints_and_quotes():
    manifest = json.loads((ROOT / "data/official_sources/open_2026/manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        content = (ROOT / item["document_path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
    from scripts.acquire_open_2026 import visible_text
    for item in manifest:
        if "competition_id" not in item:
            continue
        comp = db.get_competition(item["competition_id"])
        text = visible_text((ROOT / item["document_path"]).read_bytes())
        for cite in comp.evidence:
            if cite.document_name.endswith(".html"):
                assert " ".join(cite.source_text.split()) in text


def test_planning_completes_without_writing_profile_or_projects(planner_user):
    before = db.get_user_profile(planner_user).model_dump()
    result = run_agent("每周20小时，最多参加两场比赛", user_id=planner_user, task_mode=True)
    assert result["planning"]["status"] == "completed"
    assert len(result["recommendations"]) <= 2
    assert result["citations"]
    assert db.list_user_projects(planner_user) == []
    assert db.get_user_profile(planner_user).model_dump() == before
    assert all(step["status"] == "succeeded" for step in result["trace"])

    chinese = run_planning_task("每周不超过十小时，只参加两场比赛，目标是获奖", user_id=planner_user)
    assert chinese["planning"]["constraints"]["weekly_hours"] == 10
    assert chinese["planning"]["constraints"]["max_competitions"] == 2
    assert chinese["planning"]["constraints"]["goal"] == "award"
    assert "本轮每周预算 10 小时" in chinese["answer"]
    assert db.get_user_profile(planner_user).model_dump() == before


def test_zero_capacity_never_invents_time_or_a_feasible_plan(planner_user):
    result = run_planning_task("每周0小时，最多参加两场比赛", user_id=planner_user)
    assert result["planning"]["status"] == "needs_input"
    assert not result["recommendations"]
    assert all(not p["items"] for p in result["planning"]["plans"])
    assert db.get_user_profile(planner_user).weekly_available_hours == 20

    fractional = run_planning_task("每周10.5小时，最多参加两场比赛", user_id=planner_user)
    assert fractional["planning"]["status"] == "needs_input"
    assert not fractional["recommendations"]
    assert "每周可投入时间请使用整数小时" in fractional["answer"]

    excessive = run_planning_task("每周十小时，最多参加五场比赛", user_id=planner_user)
    assert excessive["planning"]["status"] == "needs_input"
    assert not excessive["recommendations"]
    assert "最多参赛数量须在1—4场之间" in excessive["answer"]


def test_expired_target_stops_before_eligibility_or_optimization(planner_user):
    result = run_planning_task("帮我规划", user_id=planner_user, competition_id="china_softcup_2026")
    assert [d["tool"] for d in result["planning"]["decisions"]] == ["catalog_search", "verify_evidence"]
    assert not result["recommendations"]
    assert any("不可报名" in reason for item in result["planning"]["excluded"] for reason in item["reasons"])


def test_unknown_target_cannot_be_replaced_with_other_opportunities(planner_user):
    result = run_planning_task("帮我规划宇宙无敌杯", user_id=planner_user, competition_id="nonexistent_2026")
    assert result["planning"]["status"] == "needs_input"
    assert not result["recommendations"]


def test_team_constraint_blocks_official_team_track(planner_user):
    result = run_planning_task("我们4人，每周20小时", user_id=planner_user, competition_id="aic_interconnect_2026")
    assert not result["recommendations"]
    assert any("超过上限" in reason for item in result["planning"]["excluded"] for reason in item["reasons"])
    assert db.get_user_profile(planner_user).expected_team_size == 1


def test_graduation_statement_blocks_student_only_tasks(planner_user):
    result = run_planning_task("我已经毕业，每周20小时，帮我规划", user_id=planner_user)
    assert not result["recommendations"]
    assert any("学历层次" in reason for item in result["planning"]["excluded"] for reason in item["reasons"])
    assert db.get_user_profile(planner_user).education_level.value == "本科生"


def test_existing_workload_outside_requested_category_is_preserved(planner_user):
    db.create_user_project(planner_user, "swb_autumn_2026")
    from portfolio.optimizer import optimize_portfolios
    with patch("agent.planner.optimize_portfolios", wraps=optimize_portfolios) as optimizer:
        run_planning_task("每周20小时，规划程序设计比赛", user_id=planner_user)
    assert "swb_autumn_2026" in {c.competition_id for c in optimizer.call_args.args[1]}


def test_model_can_select_a_different_valid_tool_order_after_observation(planner_user):
    responses = iter(['{"tool":"optimize_portfolio"}', '{"tool":"build_checklist"}'])
    with patch("agent.planner.llm.is_llm_enabled", return_value=True), patch("agent.planner.llm._post_chat", side_effect=lambda *a, **k: next(responses)) as model:
        result = run_planning_task("比较机会再规划", user_id=planner_user)
    decisions = result["planning"]["decisions"]
    assert [d["tool"] for d in decisions][3:5] == ["optimize_portfolio", "build_checklist"]
    assert decisions[4]["available_tools"] == ["compare_opportunities", "build_checklist"]
    assert model.call_count == 2
    assert result["planning"]["status"] == "completed"


def test_unregistered_tools_and_extra_arguments_are_rejected():
    with patch("agent.planner.llm.is_llm_enabled", return_value=True), patch("agent.planner.llm._post_chat", return_value='{"tool":"delete_profile", "code":"arbitrary"}'):
        tool, selector = choose_tool("规划", ["compare_opportunities", "optimize_portfolio"], [])
    assert tool == "compare_opportunities"
    assert selector == "policy_fallback"

    with patch("agent.planner.llm.is_llm_enabled", return_value=True), patch("agent.planner.llm._post_chat", return_value='```json\n{"tool":"optimize_portfolio",}\n```'):
        tool, selector = choose_tool("规划", ["compare_opportunities", "optimize_portfolio"], [])
    assert tool == "optimize_portfolio"
    assert selector == "model_repaired"


def test_planner_never_sends_raw_question_or_private_profile_to_model():
    with patch("agent.planner.llm.is_llm_enabled", return_value=True), patch("agent.planner.llm._post_chat", return_value='{"tool":"compare_opportunities"}') as model:
        choose_tool("我的手机号13800138000，学号private123，请比较", ["compare_opportunities", "optimize_portfolio"], [])
    prompt = model.call_args.args[1]
    assert "13800138000" not in prompt and "private123" not in prompt


def test_tool_failure_removes_all_adoptable_recommendations(planner_user):
    with patch("agent.planner.optimize_portfolios", side_effect=RuntimeError("internal secret")):
        result = run_planning_task("规划参赛", user_id=planner_user)
    assert not result["recommendations"]
    assert result["planning"]["plans"] == []
    assert result["trace"][-1]["status"] == "failed"
    assert "internal secret" not in json.dumps(result)


def test_production_rejects_unsafe_configuration_without_disclosing_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "private-short-value")
    monkeypatch.setenv("ADMIN_API_TOKEN", "private-short-value")
    monkeypatch.setenv("DEV_ADMIN_QUICK_LOGIN", "1")
    with pytest.raises(RuntimeError) as error:
        validate_production_configuration()
    assert "private-short-value" not in str(error.value)
    assert "AUTH_TOKEN_SECRET" in str(error.value)


def test_production_accepts_independent_long_secrets(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "auth-public-fixture-32-distinct-characters")
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-public-fixture-32-different-characters")
    monkeypatch.setenv("DEV_ADMIN_QUICK_LOGIN", "0")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://campus.example.org")
    validate_production_configuration()


def test_planning_api_preserves_session_ownership():
    with TestClient(api.app) as client:
        login = client.post("/api/auth/login", json={"username": "test", "password": "test123"}).json()
        token = login["access_token"]
        assert client.get("/api/agent/ask", params={"question": "规划", "task_mode": True, "user_id": "test"}).status_code == 401
        assert client.get("/api/agent/ask", params={"question": "规划", "task_mode": True, "user_id": "someone_else"}, headers={"Authorization": "Bearer " + token}).status_code == 403


def test_production_disables_public_demo_login(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENABLE_DEMO_LOGIN", raising=False)
    with pytest.raises(api.HTTPException) as error:
        api.auth_login(api.AuthLoginPayload(username="test", password="test123"))
    assert error.value.status_code == 403
