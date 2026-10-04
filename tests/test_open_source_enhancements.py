"""Generated inputs exercise constraint parsing, citation boundaries and tool selection."""
import json
from unittest.mock import patch

import cn2an
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st
import pytest

import db
import api
from agent.graph import _resolve_competition_versioned, run_agent
from agent.name_suggestions import suggest_names
from agent.planner import _constraints, choose_tool
from rag.store import CompetitionRAG
from schemas import Competition

GENERATED = settings(max_examples=80, derandomize=True, deadline=None, database=None)


@GENERATED
@given(hours=st.integers(0, 168), team=st.integers(1, 10), count=st.integers(1, 4))
def test_chinese_and_arabic_constraints_agree(hours, team, count):
    arabic = _constraints(f"每周最多{hours}小时，团队{team}人，最多参加{count}场比赛")
    chinese = _constraints(f"每周最多{cn2an.an2cn(hours)}小时，团队{cn2an.an2cn(team)}人，最多参加{cn2an.an2cn(count)}场比赛")
    assert arabic == chinese
    assert arabic["weekly_hours"] == hours
    assert arabic["team_size"] == team
    assert arabic["max_competitions"] == count


@GENERATED
@given(question=st.text(max_size=150), top_k=st.integers(-5, 15))
def test_generated_queries_keep_original_citations_and_limits(question, top_k):
    comp = db.get_competition("mathorcup_data_2026")
    rag = object.__new__(CompetitionRAG)
    hits = rag._local_query(comp, question, top_k)
    original = [item.model_dump() for item in comp.evidence]
    assert len(hits) <= max(0, top_k)
    assert all(hit.model_dump() in original for hit in hits)


@GENERATED
@given(year=st.integers(1900, 2200).filter(lambda year: year != 2026), question=st.text(max_size=50))
def test_generated_wrong_years_never_retrieve_evidence(year, question):
    rag = object.__new__(CompetitionRAG)
    assert rag.query("mathorcup_data_2026", question, document_year=year) == []


JSON_VALUES = st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=30),
                          lambda child: st.lists(child, max_size=4) | st.dictionaries(st.text(max_size=15), child, max_size=4),
                          max_leaves=10)


@GENERATED
@given(value=JSON_VALUES)
def test_generated_tool_payloads_cannot_escape_allowlist(value):
    allowed = ["compare_opportunities", "optimize_portfolio"]
    with patch("agent.planner.llm.is_llm_enabled", return_value=True), patch("agent.planner.llm._post_chat", return_value=json.dumps({"tool": value})):
        tool, selector = choose_tool("规划", allowed, [])
    assert tool in allowed
    if value not in allowed:
        assert selector == "policy_fallback"


@GENERATED
@given(response=st.text(max_size=200))
def test_generated_malformed_model_outputs_fall_back_safely(response):
    allowed = ["compare_opportunities", "optimize_portfolio"]
    with patch("agent.planner.llm.is_llm_enabled", return_value=True), patch("agent.planner.llm._post_chat", return_value=response):
        tool, _ = choose_tool("规划", allowed, [])
    assert tool in allowed


@pytest.mark.parametrize("query", ["2026年蓝乔杯什么时候报名", "请问蓝乔杯怎么参加", "蓝桥杯大赛报名"])
def test_name_typo_requires_confirmation_before_answering_rules(query):
    comps = [Competition(competition_id="name_fixture", competition_name="蓝桥杯全国软件和信息技术专业人才大赛", document_year=2026, category="programming")]
    if "蓝乔" in query:
        resolved, _, clarification = _resolve_competition_versioned(query, comps)
        assert resolved is None
        assert "蓝桥杯" in clarification and "确认" in clarification
    else:
        assert suggest_names(query, comps) == []


def test_typo_answer_contains_no_unconfirmed_deadline_or_citations():
    result = run_agent("2026年蓝乔杯什么时候报名")
    assert "确认" in result["answer"]
    assert "蓝桥杯" in result["answer"]
    assert not result["citations"]


def test_full_name_typo_is_not_overridden_by_broad_topic_matching():
    comp = Competition(competition_id="typo_modeling", competition_name="全国大学生数学建模竞赛", document_year=2026, category="modeling")
    resolved, _, clarification = _resolve_competition_versioned("全果大学生数学建模竞赛报名截止", [comp])
    assert resolved is None
    assert "全国大学生数学建模竞赛" in clarification and "确认" in clarification


def test_bm25_handles_empty_single_and_nonmatching_sources():
    from rag.lexical import scores
    assert scores("报名", ()) == []
    assert scores("报名", (("!!!", "unknown"),)) == [0.0]
    assert scores("报名截止", (("报名于十月截止", "registration_deadline"),))[0] > 0
    assert scores("xyzqwerty", (("报名于十月截止", "registration_deadline"),)) == [0.0]


def test_index_refreshes_when_source_quote_changes():
    from rag.lexical import scores
    assert scores("机器人", (("机器人比赛通知", "unknown"),))[0] > 0
    assert scores("机器人", (("数学比赛通知", "unknown"),)) == [0.0]


@settings(max_examples=30, derandomize=True, deadline=None, database=None)
@given(question=st.text(st.characters(exclude_categories=("Cs",)), max_size=60),
       top_k=st.integers(-2, 12), year=st.sampled_from([2025, 2026, 2027]))
def test_generated_http_retrieval_parameters_respect_schema_and_provenance(question, top_k, year):
    client = TestClient(api.app)
    response = client.get("/api/rag/search", params={"competition_id": "mathorcup_data_2026", "q": question, "top_k": top_k, "year": year})
    if not 1 <= top_k <= 10:
        assert response.status_code == 422
        return
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) <= top_k
    if year != 2026:
        assert hits == []
    else:
        original = [item.model_dump(mode="json") for item in db.get_competition("mathorcup_data_2026").evidence]
        assert all(hit in original for hit in hits)


def test_health_identifies_actual_offline_retrieval():
    response = TestClient(api.app).get("/health")
    assert response.status_code == 200
    assert response.json()["retrieval_backend"] == "jieba+bm25plus"
