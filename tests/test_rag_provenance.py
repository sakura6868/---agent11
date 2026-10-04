"""All retrieval backends keep source quotations and version boundaries intact."""

from types import SimpleNamespace
from unittest.mock import patch

import db
from agent.graph import _detail_fallback_citations, node_retrieve, run_agent
from rag import store


def local_rag():
    rag = object.__new__(store.CompetitionRAG)
    rag.client = None
    rag.backend = "none"
    return rag


def test_rag_never_labels_generated_summaries_as_source_quotes():
    comp = db.get_competition("mathorcup_data_2026")
    rag = local_rag()
    blocks = rag._build_blocks(comp)
    assert [block.model_dump() for block in blocks] == [item.model_dump() for item in comp.evidence]
    assert rag._build_blocks(comp.model_copy(update={"evidence": []})) == []


def test_offline_query_honors_year_and_version_filters():
    rag = local_rag()
    with patch.object(store, "_has_real_embedding", return_value=False):
        assert rag.query("mathorcup_data_2026", "报名截止", document_year=2025) == []
        assert rag.query("mathorcup_data_2026", "报名截止", doc_version="missing") == []
        assert rag.query("mathorcup_data_2026", "报名截止", document_year=2026)


def test_ingestion_invalidates_only_the_changed_competition_cache():
    comp = db.get_competition("mathorcup_data_2026")
    with patch.dict(store._SEM_CACHE, {comp.competition_id: ([], []), "other": ([], [])}, clear=True), patch.object(store, "_has_real_embedding", return_value=False):
        assert local_rag().ingest_competition(comp) == len(comp.evidence)
        assert comp.competition_id not in store._SEM_CACHE
        assert "other" in store._SEM_CACHE


def test_remote_results_are_resolved_against_current_original_evidence():
    comp = db.get_competition("mathorcup_data_2026")
    evidence = comp.evidence[0]
    meta = {"evidence_index": 0, "document_year": comp.document_year, "doc_version": comp.doc_version, "field": evidence.field}
    response = {"documents": [[evidence.source_text]], "metadatas": [[meta]]}
    collection = SimpleNamespace(query=lambda **kwargs: response)
    rag = local_rag()
    rag.client = object()
    with patch.object(rag, "_get_collection", return_value=collection), patch.object(store, "embed", return_value=[[1.0]]):
        assert rag._query_chroma(comp.competition_id, "报名截止")[0].model_dump() == evidence.model_dump()
        response["documents"] = [["报名截止时间：2026-10-23。"]]
        assert not rag._query_chroma(comp.competition_id, "报名截止")
        response["documents"] = [[evidence.source_text]]
        response["metadatas"] = [[{**meta, "doc_version": "older"}]]
        assert not rag._query_chroma(comp.competition_id, "报名截止")


def test_agent_fallback_keeps_original_evidence_and_never_invents_one():
    comp = db.get_competition("mathorcup_data_2026")
    quotes = {item.source_text for item in comp.evidence}
    assert all(item.source_text in quotes and item.citation_id for item in _detail_fallback_citations(comp))
    assert not _detail_fallback_citations(comp.model_copy(update={"evidence": []}))


def test_missing_deadline_evidence_is_not_replaced_by_an_unrelated_citation():
    comp = db.get_competition("mathorcup_data_2026")
    comp.evidence = [item for item in comp.evidence if item.field not in {"registration_deadline", "registration_deadline_at"}]
    with patch("db.get_competition", return_value=comp):
        result = node_retrieve({"resolved_competition": comp.competition_id, "question": "报名截止是什么时候", "intent": "qa"})
        assert not result["citations"]
        answer = run_agent("报名截止是什么时候", competition_id=comp.competition_id)
    assert "缺少官方原文证据" in answer["answer"]
