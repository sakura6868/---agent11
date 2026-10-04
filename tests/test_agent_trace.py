from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import db  # noqa: E402
from agent.graph import _instrument_node, run_agent  # noqa: E402


REQUIRED_STEP_FIELDS = {
    "node",
    "label",
    "status",
    "duration_ms",
    "evidence_count",
    "data_version",
    "detail",
    "fallback",
}


class AgentTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()

    def _run_deadline_query(self) -> dict:
        # 联网补充是可选能力；关闭它可让测试只验证本地确定性闭环。
        with patch("agent.graph._web_enabled", return_value=False):
            return run_agent(
                "中国软件杯报名截止日期是什么时候",
                competition_id="china_softcup_2026",
                top_k=4,
            )

    def test_run_agent_returns_structured_audit_trace(self) -> None:
        result = self._run_deadline_query()

        self.assertRegex(result["run_id"], r"^agent_[0-9a-f]{32}$")
        self.assertTrue(result["trace"])
        self.assertEqual(result["trace"][-1]["node"], "compose")
        self.assertIn("china_softcup_2026", result["data_version"])

        for step in result["trace"]:
            self.assertEqual(set(step), REQUIRED_STEP_FIELDS)
            self.assertIn(step["status"], {"succeeded", "skipped", "degraded", "failed"})
            self.assertIsInstance(step["duration_ms"], (int, float))
            self.assertGreaterEqual(step["duration_ms"], 0)
            self.assertIsInstance(step["evidence_count"], int)
            self.assertGreaterEqual(step["evidence_count"], 0)
            self.assertTrue(step["data_version"])
            self.assertTrue(step["detail"])
            self.assertIsInstance(step["fallback"], bool)
            # 轨迹只公开业务动作，不承载提示词或模型隐藏推理字段。
            self.assertFalse({"prompt", "reasoning", "thought", "chain_of_thought"} & set(step))

        # FastAPI 可直接 JSON 序列化；旧调用方可使用同步生成的字符串轨迹。
        json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["trace_legacy"], [step["detail"] for step in result["trace"]])

    def test_trace_summary_and_metrics_are_consistent(self) -> None:
        result = self._run_deadline_query()
        summary = result["trace_summary"]
        metrics = result["metrics"]

        self.assertEqual(summary["total_steps"], len(result["trace"]))
        self.assertEqual(summary["path"], [step["node"] for step in result["trace"]])
        self.assertEqual(
            summary["succeeded_steps"]
            + summary["skipped_steps"]
            + summary["degraded_steps"]
            + summary["failed_steps"],
            summary["total_steps"],
        )
        self.assertEqual(summary["evidence_count"], len(result["citations"]))
        self.assertEqual(metrics["step_count"], len(result["trace"]))
        self.assertEqual(metrics["evidence_count"], len(result["citations"]))
        self.assertEqual(metrics["fallback_count"], summary["fallback_steps"])
        self.assertEqual(metrics["data_version"], result["data_version"])
        self.assertGreaterEqual(metrics["total_duration_ms"], 0)
        self.assertGreaterEqual(metrics["node_duration_ms"], 0)
        self.assertIn("registration_deadline", metrics["evidence_fields"])

    def test_node_exception_produces_auditable_conservative_fallback(self) -> None:
        def failing_node(_state):
            raise RuntimeError("internal details must not be exposed")

        wrapped = _instrument_node("retrieve", failing_node)
        result = wrapped(
            {
                "question": "测试",
                "trace": [],
                "citations": [],
                "data_version": "catalog:test",
                "pending_review": False,
            }
        )
        step = result["trace"][-1]

        self.assertEqual(step["node"], "retrieve")
        self.assertEqual(step["status"], "failed")
        self.assertTrue(step["fallback"])
        self.assertTrue(result["pending_review"])
        self.assertEqual(result["error"], "retrieve:RuntimeError")
        self.assertNotIn("internal details", step["detail"])


if __name__ == "__main__":
    unittest.main()
