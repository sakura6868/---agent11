"""Conservative notification extraction never invents a cutoff or education level."""

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
import db
from admin.suggest import extract_date, suggest_fields
from agent.graph import run_agent
from rag.store import get_rag
from trust import assess_source_readiness


ROOT = Path(__file__).resolve().parents[1]


def suggest(*texts):
    return suggest_fields([{"page": 1, "paragraph_index": index + 1, "text": text} for index, text in enumerate(texts)])


@pytest.mark.parametrize("text", [
    "报名开始时间为2026年10月1日。",
    "报名时间为2026年10月1日至2026年10月23日。",
    "报名截止时间为2026年10月1日至2026年10月23日。",
    "2026年10月23日举行决赛和作品展示。",
    "提交作品时间待定，2026年10月23日举行终评。",
    "报名和提交作品截止时间为2026年10月23日。",
])
def test_non_cutoff_or_ambiguous_dates_stay_unknown(text):
    result = suggest(text)
    assert result["registration_deadline"]["value"] is None
    assert result["submission_deadline"]["value"] is None


def test_explicit_deadline_dates_and_clock_have_separate_evidence_indices():
    result = suggest(
        "报名截止日期为2026年10月23日12:00，截止后不能更改报名信息。",
        "论文提交截止时间为2026-10-30 20:00:30。",
    )
    assert result["registration_deadline"] == {"value": "2026-10-23", "evidence_index": 0}
    assert result["registration_deadline_time"]["value"] == "12:00"
    assert result["submission_deadline_time"] == {"value": "20:00:30", "evidence_index": 1}


def test_conflicting_rounds_are_not_silently_resolved():
    result = suggest("初赛报名截止为2026年10月23日。", "复赛报名截止为2026年12月4日。")
    assert result["registration_deadline"]["value"] is None


@pytest.mark.parametrize("text", ["2026年2月30日", "2026-13-01", "2026-02-29"])
def test_invalid_dates_are_not_suggested(text):
    assert extract_date(text) is None
    assert suggest("报名截止为" + text)["registration_deadline"]["value"] is None


def test_unknown_education_does_not_become_undergraduate():
    assert suggest("竞赛面向在校大学生。 ")["eligible"]["value"] is None
    assert suggest("面向不具备本科学历的人士。 ")["eligible"]["value"] is None
    assert suggest("面向职业本科生。 ")["eligible"]["value"] == ["职业本科生"]
    result = suggest("竞赛面向各高校在读的研究生、本科生和专科生。")
    assert set(result["eligible"]["value"]) == {"研究生", "本科生", "专科生"}


def test_team_bounds_do_not_invent_the_other_bound():
    result = suggest("每个参赛队伍人数可为1\u20133人。")
    assert result["team"]["min"] == 1 and result["team"]["max"] == 3
    assert suggest("每队3人以上。")["team"]["max"] is None
    assert suggest("每队不超过3人。")["team"]["min"] is None
    assert not suggest("专家不超过3人。")["team"]["required"]


def test_real_official_upload_parse_confirm_rag_and_agent():
    raw = json.loads((ROOT / "data/ground_truth/samples/mathorcup_data_2026.json").read_text(encoding="utf-8"))
    raw["competition_id"] = "precision_admin_pipeline_2026"
    pdf = ROOT / "data/official_sources/mathorcup_data_2026_official.pdf"
    headers = {"X-Admin-Token": "isolated-regression-token"}
    with TemporaryDirectory(prefix="official-pipeline-") as directory, patch.object(api, "UPLOAD_DIR", Path(directory)), TestClient(api.app) as client:
        uploaded = client.post("/api/admin/upload", headers=headers, json={
            "filename": pdf.name, "content_base64": base64.b64encode(pdf.read_bytes()).decode("ascii"),
        })
        assert uploaded.status_code == 200
        metadata = uploaded.json()
        assert metadata["sha256"] == raw["evidence"][0]["document_sha256"]
        parsed = client.post("/api/admin/parse", headers=headers, json={
            "file_id": metadata["file_id"], "document_id": metadata["document_id"],
            "category": "data", "document_year": 2026,
        })
        assert parsed.status_code == 200
        assert parsed.json()["block_count"] > 0
        assert any(block["rects"] for block in parsed.json()["blocks"])
        for evidence in raw["evidence"]:
            evidence["document_id"] = metadata["document_id"]
        saved = client.post("/api/admin/competitions", headers=headers, json=raw)
        assert saved.status_code == 200
        assert saved.json()["recommendation_ready"]
        assert saved.json()["rag_chunks"] > 0
    comp = db.get_competition(raw["competition_id"])
    assert comp.registration_deadline_at.isoformat() == raw["registration_deadline_at"]
    assert assess_source_readiness(comp).ready
    hits = get_rag().query(comp.competition_id, "报名截止", top_k=3)
    assert hits
    assert all(hit.citation_id and hit.document_id and hit.document_sha256 for hit in hits)
    assert all(hit.source_text in {item.source_text for item in comp.evidence} for hit in hits)
    answer = run_agent("报名截止是什么时候", competition_id=comp.competition_id)
    assert "2026-10-23 12:00" in answer["answer"]
    assert "校园时区假设" in answer["answer"]
