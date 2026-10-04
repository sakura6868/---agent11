"""Real official cutoff times remain consistent across gates, storage and calendars."""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, MetaData, Table, create_engine, inspect, select

import api
import db
from agent.graph import run_agent
from contest_clock import CAMPUS_TIMEZONE, calendar_date
from recommendation.engine import recommend_for_user
from radar.service import run_watch
from schemas import Competition
from trust import assess_source_readiness, is_registerable_now


ROOT = Path(__file__).resolve().parents[1]


def official_competition():
    raw = json.loads((ROOT / "data/ground_truth/samples/mathorcup_data_2026.json").read_text(encoding="utf-8"))
    raw["competition_id"] = "mathorcup_data_2026"
    return Competition.model_validate(raw)


@pytest.mark.parametrize("hour,minute,expected", [(11, 59, True), (12, 0, False), (12, 1, False)])
def test_official_noon_cutoff_before_at_and_after(hour, minute, expected):
    comp = official_competition()
    current = datetime(2026, 10, 23, hour, minute, tzinfo=CAMPUS_TIMEZONE)
    assert is_registerable_now(comp, current) is expected
    result = recommend_for_user(db.DEMO_USER, [comp], current)[0]
    assert result.eligible is expected
    assert (result.score is not None) is expected
    if not expected:
        assert result.recommendation_status == "ineligible"


def test_clock_uses_equal_instants_and_rejects_naive_datetimes():
    comp = official_competition()
    assert is_registerable_now(comp, datetime(2026, 10, 23, 3, 59, tzinfo=timezone.utc))
    assert not is_registerable_now(comp, datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc))
    assert calendar_date(datetime(2026, 10, 22, 20, tzinfo=timezone.utc)) == date(2026, 10, 23)
    with pytest.raises(ValueError, match="UTC offset"):
        is_registerable_now(comp, datetime(2026, 10, 23, 11))
    assert not is_registerable_now(comp, date(2026, 10, 23))


@pytest.mark.parametrize("timestamp", ["2026-10-23T12:00:00", "2026-10-24T12:00:00+08:00", "2026-10-23T12:00:00-05:00"])
def test_exact_deadline_requires_offset_and_matching_date(timestamp):
    raw = official_competition().model_dump(mode="json")
    raw["registration_deadline_at"] = timestamp
    with pytest.raises(ValidationError):
        Competition.model_validate(raw)


def test_precision_and_claimed_official_timezone_require_evidence():
    comp = official_competition()
    assert assess_source_readiness(comp).ready
    missing = comp.model_copy(update={"evidence": [item for item in comp.evidence if item.field != "registration_deadline_at"]})
    result = recommend_for_user(db.DEMO_USER, [missing], datetime(2026, 10, 3, tzinfo=CAMPUS_TIMEZONE))[0]
    assert result.recommendation_status == "candidate_only"
    assert result.score is None
    assert not assess_source_readiness(comp.model_copy(update={"deadline_timezone_basis": "official"})).ready


def test_submission_cutoff_fallback_is_conservative_without_a_clock():
    comp = official_competition().model_copy(update={
        "registration_deadline": None, "registration_deadline_at": None,
        "submission_deadline": date(2026, 10, 23),
        "submission_deadline_at": datetime(2026, 10, 23, 12, tzinfo=CAMPUS_TIMEZONE),
        "competition_start_date": None,
    })
    assert is_registerable_now(comp, datetime(2026, 10, 23, 11, 59, tzinfo=CAMPUS_TIMEZONE))
    assert not is_registerable_now(comp, datetime(2026, 10, 23, 12, tzinfo=CAMPUS_TIMEZONE))
    assert not is_registerable_now(comp, date(2026, 10, 23))


def test_storage_project_agent_and_calendar_share_exact_cutoff():
    comp = official_competition()
    saved = db.get_competition(comp.competition_id)
    assert saved.registration_deadline == comp.registration_deadline
    assert saved.registration_deadline_at == comp.registration_deadline_at
    current = datetime(2026, 10, 23, 11, 59, tzinfo=CAMPUS_TIMEZONE)
    with patch("db.contest_now", return_value=current):
        project = db.create_user_project(db.TEST_ACCOUNT_USERNAME, comp.competition_id)
        assert project.registration_deadline_at == comp.registration_deadline_at
        content = api.export_project_calendar(project.user_id, project.project_id).body.decode("utf-8")
    assert "DTSTART:20261023T040000Z" in content
    assert "DURATION:PT1M" in content
    assert "作品提交截止" not in content.replace("\r\n ", "")
    assert all(len(line.encode("utf-8")) <= 75 for line in content.split("\r\n"))
    with patch("agent.graph.contest_now", return_value=current), patch("db.get_all_competitions", return_value=[comp]):
        answer = run_agent("2026年第七届MathorCup大数据竞赛报名什么时候截止？")
    assert "2026-10-23 12:00" in answer["answer"]
    assert "校园时区假设" in answer["answer"]
    assert "官方时区" not in answer["answer"]


def test_project_and_atomic_adoption_recheck_exact_cutoff():
    comp = official_competition()
    cutoff = comp.registration_deadline_at
    with patch("db.contest_now", return_value=cutoff):
        with pytest.raises(ValueError, match="competition_expired"):
            db.create_user_project(db.TEST_ACCOUNT_USERNAME, comp.competition_id)
        created, failures = db.create_user_projects_atomic(db.TEST_ACCOUNT_USERNAME, [comp.competition_id])
    assert not created
    assert failures


def test_ics_folding_and_newlines_do_not_corrupt_fields():
    original = "SUMMARY:" + "官方证据" * 40
    folded = api._ics_fold_line(original)
    assert all(len(line.encode("utf-8")) <= 75 for line in folded)
    assert folded[0] + "".join(line[1:] for line in folded[1:]) == original
    assert "\r" not in api._ics_escape("user\rBEGIN:VEVENT\nother")
    assert "\n" not in api._ics_escape("user\rBEGIN:VEVENT\nother")


def test_existing_sqlite_schema_is_migrated_without_date_changes():
    engine = create_engine("sqlite:///:memory:")
    new_fields = {"registration_deadline_at", "submission_deadline_at", "deadline_timezone_basis"}
    legacy = Table("competitions", MetaData(), *[
        Column(col.name, col.type, primary_key=col.primary_key, nullable=col.nullable, default=col.default)
        for col in db.CompetitionModel.__table__.columns if col.name not in new_fields
    ])
    legacy.create(engine)
    with engine.begin() as connection:
        connection.execute(legacy.insert().values(
            competition_id="legacy", competition_name="legacy", document_year=2026,
            category="software", registration_deadline=date(2026, 10, 23),
        ))
    with patch.object(db, "_engine", engine):
        db._add_missing_columns(db.CompetitionModel.__table__, "sqlite:///:memory:")
        db._add_missing_columns(db.CompetitionModel.__table__, "sqlite:///:memory:")
    assert new_fields <= {column["name"] for column in inspect(engine).get_columns("competitions")}
    with engine.connect() as connection:
        assert connection.execute(select(legacy.c.registration_deadline)).scalar_one() == date(2026, 10, 23)
        assert connection.exec_driver_sql("SELECT registration_deadline_at FROM competitions").scalar_one() is None
    engine.dispose()


def test_date_only_radar_change_invalidates_old_clock_and_source_anchors():
    comp = official_competition().model_copy(update={"competition_id": "precision_radar_boundary"})
    db.upsert_competition(comp)
    watch = db.create_source_watch(comp.competition_id, "https://example.edu/precision-notice")
    run_watch(watch["watch_id"], "报名截止时间：2026年10月23日12:00")
    changed = run_watch(watch["watch_id"], "报名截止时间：2026年10月25日")
    db.review_change_event(changed["event_id"], "approve", "Synthetic regression fixture")
    updated = db.get_competition(comp.competition_id)
    assert updated.registration_deadline == date(2026, 10, 25)
    assert updated.registration_deadline_at is None
    assert not assess_source_readiness(updated).ready
    old_clock = next(item for item in updated.evidence if item.field == "registration_deadline_at")
    assert old_clock.anchor_status == "needs_review"
    snapshot = next(item for item in updated.evidence if item.field == "registration_deadline")
    assert snapshot.document_sha256 is None and snapshot.document_id is None
    assert snapshot.anchor_status == "needs_review"
