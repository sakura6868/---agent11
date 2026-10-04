"""Curated autumn evidence and unknown-date cleanup regressions."""
import importlib.util
import json
import hashlib
from datetime import datetime
from pathlib import Path

from schemas import Competition
from trust import assess_recommendation_readiness

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("data_quality", ROOT / "scripts/improve_data_2026.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def load(cid):
    return quality.validated(json.loads((ROOT / "data/ground_truth/samples" / (cid + ".json")).read_text(encoding="utf-8")))


def test_curated_autumn_events_do_not_move_old_chinese_track_to_autumn():
    now = datetime.fromisoformat("2026-10-03T17:00:00+08:00")
    for cid in ("swb_autumn_2026", "apmcm_autumn_2026"):
        assert assess_recommendation_readiness(load(cid), now).ready
    assert load("apmcm_2026").competition_start_date.isoformat() == "2026-06-12"
    assert "中文" in load("apmcm_2026").competition_name


def test_precise_swb_registration_is_distinct_from_submission():
    comp = load("swb_autumn_2026")
    assert comp.registration_deadline_at.isoformat() == "2026-11-20T06:00:00+08:00"
    assert comp.submission_deadline_at.isoformat() == "2026-11-24T10:00:00+08:00"
    assert not assess_recommendation_readiness(comp, comp.registration_deadline_at).ready


def test_dmt_unknown_minimum_and_conditional_graduates_remain_candidates():
    comp = load("dmt_2026")
    assert comp.team_min is None and comp.team_max == 5
    assert comp.allowed_grades is None
    assert all("毕业" not in level.value for level in comp.eligible_students)
    assert comp.registration_deadline_at.second == 59
    assert not assess_recommendation_readiness(comp).ready


def test_only_unsupported_candidate_deadlines_are_cleared():
    raw = {"competition_id": "unknown", "competition_name": "Unknown", "document_year": 2026,
           "category": "modeling", "registration_deadline": "2099-01-01", "data_status": "unverified"}
    assert quality.clean_candidate_dates(raw) == {"registration_deadline": "2099-01-01"}
    assert raw["registration_deadline"] is None
    raw["data_status"] = "verified"
    raw["registration_deadline"] = "2099-01-01"
    assert quality.clean_candidate_dates(raw) == {}


def test_autumn_sources_and_evidence_fingerprints():
    for entry in json.loads((ROOT / "data/official_sources/autumn_2026/manifest.json").read_text(encoding="utf-8")):
        path = ROOT / entry["document_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    for cid in ("swb_autumn_2026", "apmcm_autumn_2026", "dmt_2026"):
        for item in load(cid).evidence:
            path = ROOT / "data/official_sources/autumn_2026" / item.document_name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item.document_sha256
