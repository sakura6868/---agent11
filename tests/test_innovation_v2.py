from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from uuid import uuid4

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import api  # noqa: E402
import db  # noqa: E402
from radar.service import run_watch  # noqa: E402
from schemas import (  # noqa: E402
    Citation,
    Competition,
    CompetitionCategory,
    DataStatus,
    EducationLevel,
    Grade,
    ProjectItemStatus,
    TrustedLevel,
)
from trust import REQUIRED_EVIDENCE_FIELDS  # noqa: E402


def _ready_comp(cid: str) -> Competition:
    checked = date.today().isoformat()
    url = f"https://example.edu/{cid}.pdf"
    return Competition(
        competition_id=cid,
        competition_name=f"V2 测试赛事 {cid}",
        document_year=date.today().year,
        category=CompetitionCategory.SOFTWARE,
        eligible_students=[EducationLevel.UNDERGRADUATE],
        allowed_grades=[Grade.SOPHOMORE],
        team_required=True,
        team_min=2,
        team_max=4,
        registration_deadline=date.today() + timedelta(days=60),
        submission_deadline=date.today() + timedelta(days=90),
        required_materials=["作品说明书"],
        required_skills=["Python"],
        official_source_url=url,
        source_acquired_date=checked,
        official_source_status="found",
        last_verified_at=checked,
        data_status=DataStatus.VERIFIED,
        trusted_level=TrustedLevel.A,
        evidence=[
            Citation(
                field=field,
                page=1,
                source_text=f"官方原文 {field}",
                document_name=f"{cid}.pdf",
                source_url=url,
                acquired_date=checked,
                last_verified_at=checked,
                trusted_level=TrustedLevel.A,
            )
            for field in REQUIRED_EVIDENCE_FIELDS
        ],
    )


class RadarControlTowerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_rules_health_field_diff_and_noise_inbox_contract(self):
        cid = f"v2_radar_{uuid4().hex}"
        comp = _ready_comp(cid)
        db.upsert_competition(comp)
        watch = db.create_source_watch(
            cid,
            f"https://example.edu/{cid}",
            include_selector="#notice",
            exclude_selector=".footer",
            ignore_regex=r"^更新时间",
            trigger_terms=["截止"],
            fetch_mode="http",
            timezone="Asia/Shanghai",
        )
        self.addCleanup(self._cleanup, cid, watch["watch_id"])
        baseline = '<main id="notice"><p>报名截止时间：2026年09月30日</p><p>更新时间：旧</p></main>'
        changed = '<main id="notice"><p>报名截止时间调整为：2026年09月20日</p><p>更新时间：新</p></main>'
        noise = '<main id="notice"><p>报名截止时间调整为：2026年09月20日</p><p>新闻轮播：校园开放日</p></main>'
        self.assertEqual(run_watch(watch["watch_id"], baseline)["status"], "baseline_created")
        event = run_watch(watch["watch_id"], changed)
        self.assertEqual(event["status"], "change_detected")
        self.assertIn("registration_deadline", event["affected_fields"])
        self.assertTrue(event["field_changes"])
        self.assertEqual(run_watch(watch["watch_id"], noise)["status"], "noise_filtered")
        stored = db.get_source_watch(watch["watch_id"])
        self.assertEqual(stored["include_selector"], "#notice")
        self.assertGreaterEqual(stored["health_score"], 80)

    @staticmethod
    def _cleanup(cid: str, watch_id: int):
        with db.session_scope() as session:
            session.query(db.SourceChangeEventModel).filter_by(watch_id=watch_id).delete()
            session.query(db.SourceSnapshotModel).filter_by(watch_id=watch_id).delete()
            session.query(db.SourceWatchModel).filter_by(watch_id=watch_id).delete()
            session.query(db.CitationModel).filter_by(competition_id=cid).delete()
            session.query(db.CompetitionModel).filter_by(competition_id=cid).delete()


class ExecutionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_project_template_dependencies(self):
        cid = f"v2_project_{uuid4().hex}"
        db.upsert_competition(_ready_comp(cid))
        self.addCleanup(self._cleanup, cid)
        project = db.create_user_project("test", cid)
        phases = [item.phase for item in project.items if item.item_type.value == "task"]
        self.assertTrue({"qualification", "team_topic", "solution", "production", "submission", "defense"}.issubset(phases))
        task_items = [item for item in project.items if item.item_type.value == "task"]
        self.assertIsNone(task_items[0].depends_on_item_id)
        self.assertEqual(task_items[1].depends_on_item_id, task_items[0].item_id)
        by_phase = {item.phase: item for item in task_items}
        self.assertEqual(by_phase["defense"].depends_on_item_id, by_phase["submission"].item_id)
        for material in [item for item in project.items if item.item_type.value == "material"]:
            self.assertEqual(material.depends_on_item_id, by_phase["production"].item_id)
        # 重复读取项目不得把材料误认为旧阶段任务，更不能重建提交节点。
        refreshed = db.get_user_project("test", project.project_id)
        refreshed_by_phase = {item.phase: item for item in refreshed.items if item.item_type.value == "task"}
        self.assertEqual(refreshed_by_phase["submission"].item_id, by_phase["submission"].item_id)
        with self.assertRaises(ValueError):
            db.update_project_item("test", project.project_id, task_items[1].item_id, title=None, due_date=None, status=ProjectItemStatus.DONE)
        first = db.update_project_item("test", project.project_id, task_items[0].item_id, title=None, due_date=None, status=ProjectItemStatus.DONE)
        self.assertEqual(first.status.value, "done")

    @staticmethod
    def _cleanup(cid: str):
        with db.session_scope() as session:
            projects = session.query(db.UserProjectModel).filter_by(competition_id=cid).all()
            for project in projects:
                session.delete(project)
            session.query(db.CitationModel).filter_by(competition_id=cid).delete()
            session.query(db.CompetitionModel).filter_by(competition_id=cid).delete()

    def test_quality_and_agent_run_history_are_separate_from_frozen_metrics(self):
        run_id = db.save_agent_run({
            "run_id": f"test_run_{uuid4().hex}",
            "user_id": "test",
            "question": "测试问题",
            "intent": "qa",
            "metrics": {"total_duration_ms": 12.5},
            "trace": [],
        })
        self.assertEqual(db.get_agent_run(run_id)["question"], "测试问题")
        self.assertTrue(any(item["run_id"] == run_id for item in db.list_agent_runs("test")))
        quality = api.quality_dashboard()
        self.assertIn("golden_set", quality)
        self.assertIn("runtime", quality)
        self.assertIn("regression", quality)
        self.assertGreaterEqual(quality["regression"]["tests_run"], 48)
        # 质量接口读取的是上一次冻结报告；本轮回归尚未写回前，不能要求其
        # 与正在执行的套件互相递归地等值。
        self.assertLessEqual(quality["regression"]["passed"], quality["regression"]["tests_run"])


if __name__ == "__main__":
    unittest.main()
