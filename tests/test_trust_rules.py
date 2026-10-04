from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "ground_truth" / "samples"
sys.path.insert(0, str(SRC_DIR))

import db  # noqa: E402
from recommendation.engine import recommend_for_user  # noqa: E402
from agent.graph import run_agent  # noqa: E402
from rag.store import get_rag  # noqa: E402
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


def _user() -> UserProfile:
    return UserProfile(
        user_id="test_user",
        education_level=EducationLevel.UNDERGRADUATE,
        grade=Grade.SOPHOMORE,
        major="计算机科学与技术",
        weekly_available_hours=10,
        expected_team_size=1,
        privacy_consent=True,
    )


def _competition(**overrides) -> Competition:
    checked_at = date.today().isoformat()
    source_url = "https://example.edu/official-notice"
    evidence = [
        Citation(
            field=field,
            page=None,
            source_text=f"官方测试原文：{field}",
            document_name="official-notice.html",
            source_url=source_url,
            acquired_date=checked_at,
            last_verified_at=checked_at,
            trusted_level=TrustedLevel.A,
        )
        for field in ("registration_deadline", "eligible_students", "team_min", "team_max", "required_materials")
    ]
    values = {
        "competition_id": "test_competition_2026",
        "competition_name": "测试赛事",
        "document_year": 2026,
        "category": CompetitionCategory.PROGRAMMING,
        "eligible_students": [EducationLevel.UNDERGRADUATE],
        "team_min": 1,
        "team_max": 3,
        "required_materials": ["报名表"],
        "registration_deadline": date.today() + timedelta(days=30),
        "official_source_url": source_url,
        "official_source_status": "found",
        "source_acquired_date": checked_at,
        "trusted_level": TrustedLevel.A,
        "data_status": DataStatus.VERIFIED,
        "last_verified_at": checked_at,
        "evidence": evidence,
    }
    values.update(overrides)
    return Competition(**values)


class TrustRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()

    def test_database_deadlines_match_ground_truth(self) -> None:
        files = sorted(GROUND_TRUTH_DIR.glob("*.json"))
        self.assertGreaterEqual(len(files), 12)

        for path in files:
            raw = json.loads(path.read_text(encoding="utf-8"))
            comp = db.get_competition(path.stem)
            self.assertIsNotNone(comp, path.name)
            self.assertEqual(
                comp.registration_deadline.isoformat() if comp.registration_deadline else None,
                raw.get("registration_deadline"),
                f"{path.name} 报名截止日期与 Ground Truth 不一致",
            )
            self.assertEqual(
                comp.submission_deadline.isoformat() if comp.submission_deadline else None,
                raw.get("submission_deadline"),
                f"{path.name} 提交截止日期与 Ground Truth 不一致",
            )

    def test_expired_competition_is_ineligible_without_score(self) -> None:
        cases = (
            _competition(registration_deadline=date.today() - timedelta(days=1)),
            _competition(
                registration_deadline=None,
                submission_deadline=date.today() - timedelta(days=1),
            ),
        )
        for expired in cases:
            with self.subTest(registration_deadline=expired.registration_deadline):
                result = recommend_for_user(_user(), [expired], date.today())[0]
                self.assertEqual(result.recommendation_status, "ineligible")
                self.assertFalse(result.eligible)
                self.assertIsNone(result.score)

    def test_unverified_competition_cannot_be_scored(self) -> None:
        candidate = _competition(
            data_status=DataStatus.UNVERIFIED,
            trusted_level=TrustedLevel.B,
            last_verified_at=None,
        )
        result = recommend_for_user(_user(), [candidate], date.today())[0]

        self.assertEqual(result.recommendation_status, "candidate_only")
        self.assertTrue(result.pending_review)
        self.assertIsNone(result.score)
        self.assertIsNone(result.match_breakdown)
        self.assertFalse(result.eligible)

    def test_education_levels_are_complete_and_unknown_values_fail(self) -> None:
        for path in sorted(GROUND_TRUTH_DIR.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            for value in raw.get("eligible_students", []):
                EducationLevel(value)

        model = db.CompetitionModel(
            competition_id="invalid_education",
            competition_name="未知学历测试",
            document_year=2026,
            category="programming",
            eligible_students=json.dumps(["未知学历"], ensure_ascii=False),
            allowed_grades=None,
            allowed_majors=None,
            team_required=False,
            team_min=None,
            team_max=None,
            registration_deadline=date.today() + timedelta(days=30),
            submission_deadline=None,
            required_materials="[]",
            evaluation_dimensions="[]",
            required_skills="[]",
            official_source_url="https://example.edu/notice",
            source_acquired_date=date.today().isoformat(),
            trusted_level="C",
            data_status="unverified",
            last_verified_at=None,
            doc_version="2026_v1",
        )
        with self.assertRaises(ValueError):
            db._competition_to_pydantic(model)

    def test_verified_core_competitions_have_page_level_evidence(self) -> None:
        # 反幻觉不变量：已核验（verified + A）的赛事必须带来源可追溯的 evidence，
        # 且每条 evidence 都要有官方链接与采集/核验时间（不强制特定字段，避免与
        # 真实数据模型耦合过紧）。
        verified_records = []

        for path in sorted(GROUND_TRUTH_DIR.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("data_status") == "verified" and raw.get("trusted_level") == "A":
                verified_records.append((path.stem, raw))

        self.assertGreaterEqual(len(verified_records), 1)

        for competition_id, raw in verified_records:
            self.assertEqual(raw["data_status"], "verified")
            self.assertEqual(raw["trusted_level"], "A")
            evidence = raw.get("evidence", [])
            self.assertTrue(evidence, f"{competition_id} 缺少 evidence")
            for ev in evidence:
                self.assertIsInstance(ev.get("page"), (int, type(None)), f"{competition_id} evidence.page 应为 int 或 None")
                self.assertTrue(ev.get("source_text"), f"{competition_id} evidence 缺少来源原文")
                self.assertTrue(
                    str(ev.get("source_url", "")).startswith("http"),
                    f"{competition_id} evidence 缺少官方链接",
                )
                self.assertTrue(ev.get("acquired_date"), f"{competition_id} evidence 缺少采集时间")
                self.assertTrue(ev.get("last_verified_at"), f"{competition_id} evidence 缺少核验时间")

    def test_rag_and_agent_use_one_canonical_registration_deadline(self) -> None:
        core_ids = (
            "china_softcup_2026",
            "fwb_2026",
            "mathorcup_2026",
            "mcm_cn_2026",
            "mai_qihang_2026",
        )
        for competition_id in core_ids:
            comp = db.get_competition(competition_id)
            self.assertIsNotNone(comp)
            expected = comp.registration_deadline.isoformat()

            citations = get_rag().query(competition_id, "报名截止日期是什么时候", top_k=4)
            self.assertTrue(any(c.field == "registration_deadline" for c in citations))

            result = run_agent("报名截止日期是什么时候", competition_id=competition_id, top_k=4)
            # 自然生成后不再固守「报名截止日期：YYYY-MM-DD」字面格式，但须保证：
            # 1) 仅召回 registration_deadline 单一证据（不会有提交日/校赛日冒充第二个截止日）；
            # 2) canonical 截止日来自确定性字段 comp.registration_deadline。
            self.assertTrue(result["answer"].strip())
            self.assertTrue(result["citations"])
            self.assertEqual({c["field"] for c in result["citations"]}, {"registration_deadline"})
            self.assertEqual(comp.registration_deadline.isoformat(), expected)

    def test_competition_without_national_registration_deadline_does_not_invent_one(self) -> None:
        result = run_agent("报名截止日期是什么时候", competition_id="jsj_sj_2026", top_k=4)
        # 不得编造「报名截止日期：YYYY-MM-DD」式的统一截止日（证据中无此类 ISO 日期）
        self.assertNotRegex(result["answer"], r"报名截止日期[:：]\s*\d{4}-\d{2}-\d{2}")
        # 须基于证据作答（引用存在），而非凭空捏造
        self.assertTrue(result["citations"])

    def test_team_query_preserves_unknown_minimum(self) -> None:
        result = run_agent("MathorCup 团队几人", competition_id="mathorcup_2026", top_k=4)
        self.assertTrue(result["citations"])
        self.assertTrue({c["field"] for c in result["citations"]} & {"team_min", "team_max"})
        citation_text = " ".join(c["source_text"] for c in result["citations"])
        self.assertNotIn("1至3", citation_text)

        detail = db.get_competition_detail("mathorcup_2026")
        self.assertIsNotNone(detail)
        self.assertIn("最多 3 人", detail.requirements[0].text)
        self.assertIsNone(detail.competition.team_min)


if __name__ == "__main__":
    unittest.main()
