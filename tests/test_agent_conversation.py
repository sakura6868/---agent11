"""Current-turn gates, per-conversation context and field-scoped evidence."""

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
import db
import agent.graph as graph
from contest_clock import CAMPUS_TIMEZONE
from tests.test_security_and_readiness import profile


NOW = datetime(2026, 10, 3, 11, tzinfo=CAMPUS_TIMEZONE)
TARGET = "mathorcup_data_2026"


@pytest.fixture
def conversation():
    comp = db.get_competition(TARGET)
    student = profile()
    with patch.object(db, "get_all_competitions", return_value=[comp]), \
         patch.object(db, "get_competition", side_effect=lambda cid: comp if cid == TARGET else None), \
         patch.object(db, "get_user_profile", return_value=student), \
         patch.object(graph, "contest_now", return_value=NOW):
        yield comp, student


@pytest.mark.parametrize("question", ["这个比赛几个人一队？", "报名还来得及吗？"])
def test_followup_requires_explicit_conversation_context(conversation, question):
    result = graph.run_agent(question, context_competition_id=TARGET)
    assert result["resolved_competition"] == TARGET
    assert "2026" in result["answer"]
    if "几个人" in question:
        body = result["answer"].split("——— 引用来源 ———")[0]
        quote = next(c["source_text"] for c in result["citations"] if c["field"] == "team_min")
        assert body.count(quote) == 1
        assert "[1]" in body and "[2]" in body
    assert not graph.run_agent(question)["resolved_competition"]


@pytest.mark.parametrize("question", [
    "现在我们改成四个人了，还能报名吗？",
    "我们4个人可以参加吗？",
    "我们的队伍是四人，能报吗？",
])
def test_current_team_statement_overrides_only_this_answer(conversation, question):
    _, saved = conversation
    result = graph.run_agent(question, user_id=saved.user_id, competition_id=TARGET)
    assert result["gate"]["eligible"] is False
    assert not result["score"].get("total")
    assert "超过上限" in result["answer"]
    assert saved.expected_team_size == 3


def test_graduated_statement_blocks_recruitment(conversation):
    _, student = conversation
    result = graph.run_agent("帮我写组队招募，我已经毕业两年了", user_id=student.user_id, competition_id=TARGET)
    assert result["gate"]["eligible"] is False
    assert "我们正在为" not in result["answer"]
    assert "暂不符合" in result["answer"]
    assert student.education_level.value == "本科生"


def test_unknown_name_never_uses_previous_event(conversation):
    result = graph.run_agent("明年宇宙高校创新杯报名要多少钱？", context_competition_id=TARGET)
    assert result["resolved_competition"] is None
    assert not result["citations"]


def test_named_event_overrides_previous_context(conversation):
    comp, _ = conversation
    other = comp.model_copy(update={"competition_id": "other_event", "competition_name": "另一个完整赛事"})
    with patch.object(db, "get_all_competitions", return_value=[comp, other]), \
         patch.object(db, "get_competition", side_effect=lambda cid: {TARGET: comp, "other_event": other}.get(cid)):
        result = graph.run_agent("另一个完整赛事什么时候报名？", context_competition_id=TARGET)
    assert result["resolved_competition"] == "other_event"


def test_deadline_and_end_date_are_distinct_and_never_model_rewritten(conversation):
    with patch.object(graph, "_llm_generate", return_value="报名截止2026-03-01[1]") as model:
        result = graph.run_agent("报名截止和比赛结束分别是什么时候？", competition_id=TARGET)
    assert "2026-10-23 12:00" in result["answer"]
    assert "20:00" in result["answer"]
    assert "2026-03-01" not in result["answer"]
    assert {c["field"] for c in result["citations"]} == {"registration_deadline", "competition_end_date"}
    model.assert_not_called()


def test_missing_fee_is_not_replaced_by_materials(conversation):
    result = graph.run_agent("需要交多少钱？", competition_id=TARGET)
    assert "无法确认" in result["answer"]
    assert not result["citations"]
    assert "论文" not in result["answer"]


def test_explicit_data_track_alias_and_relative_year(conversation):
    result = graph.run_agent("MathorCup大数据今年怎么报名？")
    assert result["resolved_competition"] == TARGET
    missing = graph.run_agent("MathorCup大数据明年什么时候报名？")
    assert missing["resolved_competition"] is None
    assert "年份" in missing["answer"]


def test_one_week_candidate_advice_keeps_trust_notice(conversation):
    comp, _ = conversation
    candidate = comp.model_copy(update={"data_status": "unverified", "trusted_level": "B"})
    with patch.object(db, "get_competition", return_value=candidate):
        result = graph.run_agent("帮我制定一周备赛计划", competition_id=TARGET)
    assert result["pending_review"]
    assert "第7天" in result["answer"]
    assert "候选" in result["answer"]
    assert "3–6个月" not in result["answer"]


@pytest.mark.parametrize("answer", ["你符合报名条件，可以参赛[1]", "资格肯定没问题[99]"])
def test_model_cannot_override_gate_or_invent_citations(conversation, answer):
    comp, student = conversation
    state = {"question": "如何准备", "user_id": student.user_id,
             "gate": {"eligible": False, "reasons": ["人数超过上限"]}, "pending_review": False}
    with patch.object(graph, "_llm_generate", return_value=answer):
        assert graph._generate_grounded(state, "chat", [comp.evidence[0].model_dump(mode="json")], comp.competition_name) is None


def test_api_accepts_context_and_model_status_matches_configuration(conversation):
    with TestClient(api.app) as client:
        result = client.get("/api/agent/ask", params={"question": "这个比赛几个人一队？", "context_competition_id": TARGET})
        assert result.status_code == 200
        assert result.json()["resolved_competition"] == TARGET
        status = client.get("/api/agent/llm-status").json()
        assert status["models"] == [status["model"]]
        assert client.get("/api/agent/ask", params={"question": "如何备赛", "model": "unconfigured-expensive-model"}).status_code == 400
