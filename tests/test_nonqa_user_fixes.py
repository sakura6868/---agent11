"""Non-conversation regressions found in synthetic student journeys."""

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import hashlib
import json
import sys

import pytest
from fastapi.testclient import TestClient

import api
import db
from contest_clock import CAMPUS_TIMEZONE
from recommendation.engine import eligibility_gate
from schemas import EducationLevel, Grade, ProjectItemStatus
from tests.test_security_and_readiness import ready_competition, profile
from trust import assess_recommendation_readiness, is_registerable_now

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 11, tzinfo=CAMPUS_TIMEZONE)


@pytest.fixture
def student_event():
    uid = "nonqa_" + uuid4().hex[:20]
    student = profile().model_copy(update={"user_id": uid})
    comp = ready_competition().model_copy(update={
        "competition_id": uid + "_event", "registration_deadline": date(2026, 10, 23),
        "submission_deadline": date(2026, 11, 6),
    })
    db.save_user_profile(student)
    db.create_auth_user(uid, "isolated-student-password")
    db.upsert_competition(comp)
    with patch("db.contest_now", return_value=NOW), patch("api.contest_now", return_value=NOW):
        yield student, comp
    db.delete_user_profile(uid)
    with db.session_scope() as session:
        session.query(db.AuthUser).filter_by(username=uid).delete()
        session.query(db.CitationModel).filter_by(competition_id=comp.competition_id).delete()
        session.query(db.CompetitionModel).filter_by(competition_id=comp.competition_id).delete()


@pytest.mark.parametrize("updates", [
    {"expected_team_size": 4},
    {"education_level": EducationLevel.RECENT_GRADUATE, "grade": Grade.GRADUATED},
    {"education_level": EducationLevel.POSTGRADUATE, "grade": Grade.MASTER_FIRST},
    {"grade": Grade.MASTER_FIRST},
])
def test_all_project_entrypoints_recheck_student_eligibility(student_event, updates):
    student, comp = student_event
    db.save_user_profile(student.model_copy(update=updates))
    with pytest.raises(ValueError, match="profile_ineligible"):
        db.create_user_project(student.user_id, comp.competition_id)
    with pytest.raises(ValueError, match="profile_ineligible"):
        db.create_task_from_agent(student.user_id, comp.competition_id, "准备材料")
    projects, failures = db.create_user_projects_atomic(student.user_id, [comp.competition_id])
    assert not projects and failures
    assert not db.list_user_projects(student.user_id)


def test_atomic_failure_does_not_write_eligible_part(student_event):
    student, comp = student_event
    other = comp.model_copy(update={"competition_id": comp.competition_id + "_other", "team_min": 4, "team_max": 5})
    db.upsert_competition(other)
    try:
        projects, failures = db.create_user_projects_atomic(student.user_id, [comp.competition_id, other.competition_id])
        assert not projects and len(failures) == 1
        assert not db.list_user_projects(student.user_id)
    finally:
        with db.session_scope() as session:
            session.query(db.CitationModel).filter_by(competition_id=other.competition_id).delete()
            session.query(db.CompetitionModel).filter_by(competition_id=other.competition_id).delete()


@pytest.mark.parametrize("education", [EducationLevel.VOCATIONAL_COLLEGE, EducationLevel.VOCATIONAL_COLLEGE_STUDENT, EducationLevel.JUNIOR_COLLEGE])
def test_known_college_synonyms_preserve_identity(student_event, education):
    student, comp = student_event
    student = student.model_copy(update={"education_level": education})
    comp = comp.model_copy(update={"eligible_students": [EducationLevel.JUNIOR_COLLEGE]})
    assert eligibility_gate(student, comp, NOW)[0]
    assert student.education_level == education
    assert not eligibility_gate(student.model_copy(update={"education_level": EducationLevel.SECONDARY_VOCATIONAL}), comp, NOW)[0]


def test_profile_options_and_api_accept_only_matching_grades(student_event):
    student, _ = student_event
    headers = {"Authorization": "Bearer " + api._issue_access_token(student.user_id)}
    with TestClient(api.app) as client:
        options = client.get("/api/profile/options").json()["grades_by_education"]
        assert "研一" in options["研究生"] and "大四" not in options["研究生"]
        assert options["毕业生（毕业5年内）"] == ["已毕业"]
        payload = student.model_dump(mode="json")
        payload.update(education_level="研究生", grade="研一")
        assert client.post(f"/api/users/{student.user_id}/profile", json=payload, headers=headers).status_code == 200
        payload.update(education_level="本科生")
        assert client.post(f"/api/users/{student.user_id}/profile", json=payload, headers=headers).status_code == 422
        payload.update(grade="未知")
        assert client.post(f"/api/users/{student.user_id}/profile", json=payload, headers=headers).status_code == 422


def test_unknown_legacy_grade_remains_unknown_and_blocks_projects(student_event):
    student, comp = student_event
    with db.session_scope() as session:
        session.get(db.UserProfileModel, student.user_id).grade = "legacy-unrecognised"
    saved = db.get_user_profile(student.user_id)
    assert saved.grade == Grade.UNKNOWN
    assert not eligibility_gate(saved, comp, NOW)[0]
    with pytest.raises(ValueError, match="profile_ineligible"):
        db.create_user_project(student.user_id, comp.competition_id)


def test_personal_readiness_endpoint_is_owned_and_candidates_have_no_conclusion(student_event):
    student, comp = student_event
    headers = {"Authorization": "Bearer " + api._issue_access_token(student.user_id)}
    path = f"/api/users/{student.user_id}/competitions/{comp.competition_id}/eligibility"
    with TestClient(api.app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer " + api._issue_access_token("test")}).status_code == 403
        assert client.get(path, headers=headers).json()["can_create_project"]
        db.save_user_profile(student.model_copy(update={"expected_team_size": 4}))
        assert not client.get(path, headers=headers).json()["can_create_project"]
        db.upsert_competition(comp.model_copy(update={"evidence": []}))
        payload = client.get(path, headers=headers).json()
        assert payload["eligible"] is None and not payload["can_create_project"]


def test_existing_project_preserved_after_personal_eligibility_change(student_event):
    student, comp = student_event
    project = db.create_user_project(student.user_id, comp.competition_id)
    db.save_user_profile(student.model_copy(update={"expected_team_size": 4}))
    saved = db.list_user_projects(student.user_id)[0]
    assert saved.project_id == project.project_id and saved.items
    assert not saved.recommendation_ready and saved.risk_level == "high"
    assert "预计团队人数超过上限" in saved.readiness_reasons


def test_plan_is_anchored_to_creation_not_every_read(student_event):
    student, comp = student_event
    project = db.create_user_project(student.user_id, comp.competition_id)
    assert all(not item.due_date or item.due_date >= NOW.date() for item in project.items)
    dates = [item.due_date for item in project.items if item.item_type.value == "task" and item.due_date]
    assert dates == sorted(dates)
    initial = {item.item_id: item.due_date for item in project.items}
    with patch("db.contest_now", return_value=NOW + timedelta(days=8)):
        reread = db.create_user_project(student.user_id, comp.competition_id)
    assert {item.item_id: item.due_date for item in reread.items} == initial
    assert any(item.due_date < (NOW + timedelta(days=8)).date() for item in reread.items if item.due_date)
    assert reread.registration_deadline == comp.registration_deadline


def test_completed_and_custom_task_dates_are_not_rewritten(student_event):
    student, comp = student_event
    project = db.create_user_project(student.user_id, comp.competition_id)
    task = next(item for item in project.items if item.phase == "qualification")
    with db.session_scope() as session:
        row = session.get(db.ProjectItemModel, task.item_id)
        row.status = ProjectItemStatus.DONE.value
        row.due_date = date(2026, 10, 1)
        session.add(db.ProjectItemModel(project_id=project.project_id, item_type="task", title="用户自定节点",
                                       due_date=date(2026, 10, 2), status="todo", phase="execution", sort_order=99))
    reread = db.create_user_project(student.user_id, comp.competition_id)
    assert next(item for item in reread.items if item.item_id == task.item_id).due_date == date(2026, 10, 1)
    assert next(item for item in reread.items if item.title == "用户自定节点").due_date == date(2026, 10, 2)


def test_calendar_day_uses_project_creation_in_campus_timezone(student_event):
    student, comp = student_event
    midnight = datetime(2026, 10, 4, 0, 30, tzinfo=CAMPUS_TIMEZONE)
    with patch("db.contest_now", return_value=midnight):
        project = db.create_user_project(student.user_id, comp.competition_id)
    assert all(not item.due_date or item.due_date >= date(2026, 10, 4) for item in project.items)


def test_started_production_does_not_override_explicit_open_registration(student_event):
    student, comp = student_event
    started = comp.model_copy(update={"competition_start_date": date(2026, 8, 6)})
    assert is_registerable_now(started, NOW)
    assert assess_recommendation_readiness(started, NOW).ready
    assert eligibility_gate(student, started, NOW)[0]
    assert not is_registerable_now(started, datetime(2026, 10, 24, tzinfo=CAMPUS_TIMEZONE))


def test_acquired_track_evidence_matches_immutable_html_snapshots():
    sys.path.insert(0, str(ROOT / "scripts"))
    from acquire_ncccu_2026 import visible_text
    manifest = json.loads((ROOT / "data/official_sources/ncccu_2026/manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 3
    for entry in manifest:
        content = (ROOT / entry["snapshot"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        record = db.get_competition(entry["competition_id"])
        assert assess_recommendation_readiness(record, NOW).ready
        for evidence in record.evidence:
            assert " ".join(evidence.source_text.split()) in visible_text(content)
            assert evidence.document_sha256 == entry["sha256"] and evidence.page is None
