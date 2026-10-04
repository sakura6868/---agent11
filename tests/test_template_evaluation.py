"""Generated evaluation accounting and resolution safety regressions."""

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

import db
import agent.graph as graph
from evaluation_provenance import citation_identity, competition_snapshot_sha256, evaluation_is_current
from schemas import Competition

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("template_eval", ROOT / "evals/run_eval.py")
template_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(template_eval)


def event(cid="template_fixture", name="亚太地区大学生数学建模竞赛", year=2026):
    return Competition(competition_id=cid, competition_name=name, document_year=year,
                       category="modeling", data_status="unverified")


def test_full_modeling_name_beats_topic_heuristic():
    target = event()
    national = event("national_fixture", "高教社杯全国大学生数学建模竞赛")
    result, _, clarification = graph._resolve_competition_versioned(
        "2026年亚太地区大学生数学建模竞赛报名截止是什么时候", [national, target])
    assert result.competition_id == target.competition_id
    assert clarification is None
    assert graph._classify_intent("报名截止是什么时候", True) == "qa"


def test_same_year_duplicate_needs_track_and_explicit_id_disambiguates():
    events = [event("track_first"), event("track_second")]
    result, _, clarification = graph._resolve_competition_versioned(
        "2026年亚太地区大学生数学建模竞赛团队几人", events)
    assert result is None and "赛道" in clarification
    result, _, _ = graph._resolve_competition_versioned("track_second 团队几人", events)
    assert result.competition_id == "track_second"


def test_multiple_years_are_not_silently_collapsed():
    result, _, clarification = graph._resolve_competition_versioned(
        "2025年和2026年亚太地区大学生数学建模竞赛截止", [event(year=2025), event("other", year=2026)])
    assert result is None and "多个年份" in clarification


def test_aggregate_counts_errors_as_failures_and_no_applicable_as_null():
    payload = template_eval.aggregate([
        {"passed": True, "assertions": {"no_error": True}},
        {"passed": False, "assertions": {"no_error": False}},
    ])
    assert payload["passed"] == 1 and payload["total_cases"] == 2
    assert payload["checks"]["no_error"] == {"passed": 1, "total": 2, "rate": 50.0}
    assert payload["citation_recall"] is None


def test_loading_does_not_fall_back_to_real_database():
    with TemporaryDirectory(prefix="empty-template-source-") as directory:
        with pytest.raises(ValueError, match="No Ground Truth"):
            template_eval.load_competitions(Path(directory))


def test_fact_question_not_confused_by_plan_in_official_name():
    assert graph._classify_intent("全国大学生创新创业训练计划年会团队几人", True) == "qa"
    assert graph._classify_intent("挑战杯创业计划竞赛报名什么时候截止", True) == "qa"


def test_fingerprints_preserve_source_but_ignore_surrogate_ids():
    base = {"field": "team_max", "source_text": "three", "page": 1, "citation_id": 1, "document_id": 5}
    assert citation_identity(base) == citation_identity({**base, "citation_id": 22, "document_id": 9})
    assert citation_identity(base) != citation_identity({**base, "source_text": "four"})
    original = event()
    changed = original.model_copy(update={"notes": "source changed"})
    assert competition_snapshot_sha256([original]) != competition_snapshot_sha256([changed])


def test_stale_report_cannot_show_current_metrics():
    from evaluation_provenance import code_sha256, dataset_sha256

    events = [event()]
    report = {"evaluated_at": "2026-10-03", "dataset_sha256": dataset_sha256(ROOT),
              "code_sha256": code_sha256(ROOT), "competition_snapshot_sha256": competition_snapshot_sha256(events)}
    assert evaluation_is_current(report, ROOT, events)
    assert not evaluation_is_current({**report, "code_sha256": "old"}, ROOT, events)
    assert not evaluation_is_current(report, ROOT, [event("changed")])


def test_missing_deadline_does_not_claim_official_silence():
    comp = event("missing_deadline_fixture", "模板缺失日期赛事")
    db.upsert_competition(comp)
    with patch.object(db, "get_all_competitions", return_value=[comp]):
        response = graph.run_agent("模板缺失日期赛事报名截止是什么时候", user_id="mock_user_001")
    assert "不能确认" in response["answer"]
    assert "官方文件未给出" not in response["answer"]
    assert not response["citations"]


def test_ambiguity_does_not_attach_other_version_citations():
    events = [event("ambiguous_one"), event("ambiguous_two")]
    with patch.object(db, "get_all_competitions", return_value=events):
        response = graph.run_agent("2026年亚太地区大学生数学建模竞赛团队几人")
    assert response["resolved_competition"] is None
    assert not response["citations"] and not response["web_results"]


def test_closed_source_ready_event_cannot_create_recruitment_copy():
    from datetime import date
    comp = db.get_competition("mathorcup_data_2026").model_copy(update={
        "competition_id": "closed_recruitment_fixture", "competition_name": "封闭招募测试赛事",
        "registration_deadline": date(2000, 1, 1), "registration_deadline_at": None,
    })
    db.upsert_competition(comp)
    with patch.object(db, "get_all_competitions", return_value=[comp]):
        response = graph.run_agent("封闭招募测试赛事帮我写一份组队招募文案", user_id="mock_user_001")
    assert "不处于可报名状态" in response["answer"]
    assert "正在为" not in response["answer"]


def test_new_source_is_not_counted_as_successfully_checked():
    watch = db.create_source_watch("mathorcup_data_2026", "https://example.edu/new-health-fixture")
    assert watch["health_status"] == "unknown" and watch["health_score"] == 0
    assert watch["last_success_at"] is None
    import api

    with patch.object(db, "list_source_watches", return_value=[watch]), patch.object(db, "list_change_events", return_value=[]):
        assert api.radar_status()["average_health_score"] is None
    healthy = {**watch, "health_status": "healthy", "health_score": 100}
    with patch.object(db, "list_source_watches", return_value=[watch, healthy]), patch.object(db, "list_change_events", return_value=[]):
        status = api.radar_status()
        assert status["healthy_count"] == 1 and status["average_health_score"] == 100


def test_old_success_is_degraded_and_timestamp_has_utc_offset():
    from datetime import datetime, timedelta, timezone

    watch = db.create_source_watch("mathorcup_data_2026", "https://example.edu/old-health-fixture")
    with db.session_scope() as session:
        row = session.get(db.SourceWatchModel, watch["watch_id"])
        row.last_status = "ok"
        row.last_success_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)
        row.last_checked_at = row.last_success_at
    stored = db.get_source_watch(watch["watch_id"])
    assert stored["health_status"] == "degraded" and stored["health_score"] < 80
    assert stored["last_success_at"].endswith("+00:00")


def test_code_fingerprint_is_stable_across_windows_and_linux_line_endings():
    from evaluation_provenance import code_sha256

    with TemporaryDirectory(prefix="code-fingerprint-fixture-") as directory:
        root = Path(directory)
        (root / "src").mkdir()
        path = root / "src/example.py"
        path.write_bytes(b"value = 1\r\nnext_value = 2\r\n")
        windows_hash = code_sha256(root)
        path.write_bytes(b"value = 1\nnext_value = 2\n")
        assert code_sha256(root) == windows_hash
