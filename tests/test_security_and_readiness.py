from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import api  # noqa: E402
import db  # noqa: E402
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
from trust import REQUIRED_EVIDENCE_FIELDS, assess_source_readiness  # noqa: E402


def ready_competition() -> Competition:
    checked_at = date.today().isoformat()
    source_url = "https://contest.example.edu/notice"
    evidence = [
        Citation(
            field=field,
            source_text=f"官方原文：{field}",
            document_name="notice.html",
            source_url=source_url,
            acquired_date=checked_at,
            last_verified_at=checked_at,
        )
        for field in REQUIRED_EVIDENCE_FIELDS
    ]
    return Competition(
        competition_id="security_ready_2026",
        competition_name="安全测试赛事",
        document_year=2026,
        category=CompetitionCategory.SOFTWARE,
        eligible_students=[EducationLevel.UNDERGRADUATE],
        team_required=True,
        team_min=1,
        team_max=3,
        registration_deadline=date.today() + timedelta(days=30),
        required_materials=["报名表"],
        official_source_url=source_url,
        source_acquired_date=checked_at,
        trusted_level=TrustedLevel.A,
        data_status=DataStatus.VERIFIED,
        last_verified_at=checked_at,
        official_source_status="found",
        evidence=evidence,
    )


def profile() -> UserProfile:
    return UserProfile(
        user_id="security_test_user",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SOPHOMORE,
        major="软件工程",
        expected_team_size=3,
        privacy_consent=True,
    )


def remove_security_test_competition() -> None:
    """防止管理员接口鉴权测试污染本地演示赛事库。"""
    with db.session_scope() as session:
        session.query(db.CitationModel).filter_by(competition_id="security_ready_2026").delete()
        session.query(db.CompetitionModel).filter_by(competition_id="security_ready_2026").delete()


class SecurityAndReadinessTests(unittest.TestCase):
    def test_public_identity_lists_exclude_real_profiles(self) -> None:
        real = profile().model_copy(update={"user_id": "private_registered_student", "major": "private-major"})
        with patch.object(db, "list_user_profiles", return_value=[real]):
            for users in (api.list_users(), api.student_types()):
                self.assertNotIn(real.user_id, {item["user_id"] for item in users})
                self.assertNotIn("private-major", str(users))
        first = db.list_demo_profiles()[0]
        original = first.major
        first.major = "mutated"
        self.assertEqual(db.list_demo_profiles()[0].major, original)

    def test_health_never_exposes_database_url(self) -> None:
        payload = api.health()
        serialized = str(payload).lower()
        self.assertIn(payload["database"], {"connected", "unavailable"})
        self.assertNotIn("sqlite:", serialized)
        self.assertNotIn("postgres", serialized)
        self.assertNotIn("password", serialized)
        with TestClient(api.app) as client:
            login = client.post("/api/auth/login", json={"username": "test", "password": "test123"})
            self.assertEqual(login.status_code, 200)
            token = login.json()["access_token"]
            self.assertEqual(client.get("/api/users/test/profile").status_code, 401)
            self.assertEqual(
                client.get("/api/users/test/profile", headers={"Authorization": f"Bearer {token}"}).status_code,
                200,
            )
            self.assertEqual(
                client.get("/api/users/stu_algo/profile", headers={"Authorization": f"Bearer {token}"}).status_code,
                403,
            )

    def test_all_admin_routes_require_a_valid_token(self) -> None:
        self.addCleanup(remove_security_test_competition)
        competition = ready_competition().model_dump(mode="json")
        requests = (
            ("/api/admin/upload", {"filename": "invalid.txt", "content_base64": "eA=="}),
            ("/api/admin/parse", {}),
            ("/api/admin/competitions", competition),
            ("/api/admin/competitions/bulk", {"competitions": []}),
        )
        with patch.object(api, "_ADMIN_API_TOKEN", "unit-test-secret"):
            with TestClient(api.app) as client:
                for path, body in requests:
                    with self.subTest(path=path):
                        self.assertEqual(client.post(path, json=body).status_code, 401)
                        self.assertEqual(
                            client.post(path, json=body, headers={"X-Admin-Token": "wrong"}).status_code,
                            401,
                        )
                        valid_status = client.post(
                            path, json=body, headers={"X-Admin-Token": "unit-test-secret"}
                        ).status_code
                        self.assertNotIn(valid_status, {401, 503})

    def test_admin_routes_fail_closed_without_server_configuration(self) -> None:
        with patch.object(api, "_ADMIN_API_TOKEN", ""):
            with TestClient(api.app) as client:
                response = client.post("/api/admin/upload", json={})
        self.assertEqual(response.status_code, 503)

    def test_not_found_source_cannot_be_scored(self) -> None:
        comp = ready_competition().model_copy(update={"official_source_status": "not_found"})
        result = recommend_for_user(profile(), [comp], date.today())[0]
        self.assertEqual(result.recommendation_status, "candidate_only")
        self.assertIsNone(result.score)
        self.assertFalse(result.eligible)

    def test_each_missing_evidence_field_blocks_scoring(self) -> None:
        base = ready_competition()
        for missing in REQUIRED_EVIDENCE_FIELDS:
            with self.subTest(field=missing):
                comp = base.model_copy(
                    update={"evidence": [item for item in base.evidence if item.field != missing]}
                )
                assessment = assess_source_readiness(comp)
                self.assertFalse(assessment.ready)
                result = recommend_for_user(profile(), [comp], date.today())[0]
                self.assertEqual(result.recommendation_status, "candidate_only")
                self.assertIsNone(result.score)

    def test_catalog_filters_use_shared_readiness(self) -> None:
        base = ready_competition()
        candidate = base.model_copy(update={"competition_id": "candidate", "data_status": DataStatus.UNVERIFIED})
        with patch.object(db, "list_competitions", return_value=[base, candidate]):
            ready = api.list_competitions(category=None, year=None, readiness="ready")
            candidates = api.list_competitions(category=None, year=None, readiness="candidate")
        self.assertTrue(ready)
        self.assertTrue(candidates)
        self.assertTrue(all(item["recommendation_ready"] for item in ready))
        self.assertTrue(all(not item["recommendation_ready"] for item in candidates))

    def test_invalid_evidence_metadata_cannot_be_used(self) -> None:
        base = ready_competition()
        variants = (
            {"source_text": " "}, {"source_url": "https://www.baidu.com/s?wd=test"},
            {"acquired_date": None}, {"last_verified_at": None},
            {"acquired_date": "not-a-date"}, {"last_verified_at": "2000-01-01"},
            {"trusted_level": TrustedLevel.B}, {"anchor_status": "invalid"},
            {"document_name": "notice.pdf", "page": None},
            {"document_name": "notice.pdf", "page": 0},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                evidence = [item.model_copy(update=changes) if item.field == "registration_deadline" else item for item in base.evidence]
                self.assertFalse(assess_source_readiness(base.model_copy(update={"evidence": evidence})).ready)

    def test_admin_classification_never_fabricates_metadata(self) -> None:
        comp = ready_competition()
        comp.evidence[0].acquired_date = None
        result, reasons = api._classify_submitted_competition(comp)
        self.assertEqual(result.data_status, DataStatus.UNVERIFIED)
        self.assertIsNone(result.evidence[0].acquired_date)
        self.assertTrue(reasons)

    def test_rag_write_routes_require_admin_token(self) -> None:
        with patch.object(api, "_ADMIN_API_TOKEN", "unit-test-secret"), TestClient(api.app) as client:
            for path in ("/api/rag/reseed", "/api/competitions/china_softcup_2026/ingest"):
                with self.subTest(path=path):
                    self.assertEqual(client.post(path).status_code, 401)


if __name__ == "__main__":
    unittest.main()
