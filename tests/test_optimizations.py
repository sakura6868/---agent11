from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import db  # noqa: E402
from agent.graph import (  # noqa: E402
    _alias_expand,
    _detail_fallback_citations,
    _is_meaningful_question,
    node_retrieve,
    run_agent,
)


class TestInputGuardAndGrounding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_noise_guard_marks_meaningless_input(self):
        # 纯符号/空白/超短输入应判为无意义，避免套画像产出无意义闲聊。
        self.assertFalse(_is_meaningful_question("???"))
        self.assertFalse(_is_meaningful_question("   "))
        self.assertFalse(_is_meaningful_question("。。。"))
        self.assertFalse(_is_meaningful_question("a"))
        # 含汉字/数字/字母即为有意义。
        self.assertTrue(_is_meaningful_question("蓝桥杯报名"))
        self.assertTrue(_is_meaningful_question("123"))
        self.assertTrue(_is_meaningful_question("hello"))

    def test_run_agent_noise_input_returns_friendly_nonempty(self):
        # 入口前置拦截：不崩、非空、无引用、intent=noise。
        res = run_agent("？？？？")
        self.assertEqual(res["intent"], "noise")
        self.assertTrue(res["answer"])
        self.assertEqual(res["citations"], [])
        self.assertIn("换个说法", res["answer"])

    def test_internet_plus_alias_expands_to_canonical(self):
        q = _alias_expand("互联网+大赛我一个文科生能玩吗")
        self.assertIn("中国国际大学生创新大赛", q)

    def test_chat_grounding_fallback_for_known_competition(self):
        # 已知赛事 + chat 意图 + RAG 语料为空时，应回退结构化关键字段补 [n] 引用。
        comps = db.get_all_competitions()
        target = next(
            (c for c in comps if "中国国际大学生创新大赛" in (c.competition_name or "")),
            None,
        )
        if target is None:
            self.skipTest("种子数据中未找到「中国国际大学生创新大赛」")
        state = {
            "question": "互联网+大赛我一个文科生能玩吗",
            "resolved_competition": target.competition_id,
            "intent": "chat",
            "top_k": 4,
            "trace": [],
        }
        out = node_retrieve(state)
        cites = out.get("citations") or []
        self.assertTrue(
            cites,
            "已锁定赛事的 chat 开放问法必须带官方引用（c15 grounding 修复）",
        )
        # 回退引用应源自该赛事结构化字段。
        self.assertTrue(
            any(c.get("field") for c in cites)
        )


if __name__ == "__main__":
    unittest.main()
