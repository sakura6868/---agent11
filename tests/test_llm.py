"""LLM 润色层测试：优雅降级 + 引用安全 + 正常路径。

核心保证：
  1. 未配置 API Key 时 polish 必须返回 None（回退确定性模板，不影响主链路）；
  2. 润色结果若破坏 [n] 引用编号，必须回退 None（引用安全）；
  3. 配置 Key 且端点正常时，原稿 [n] 被完整保留则返回润色文本。

注：不上 `unittest.mock.patch.dict("os.environ", ...)`，因为该写法在退出时整份还原
环境，会触发 Windows「环境变量超过 32767 字符」的限制（本机存在超长的 WorkBuddy
产品配置变量）。这里只用轻量打补器，只动被测的两个键。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import agent.llm as llm  # noqa: E402


class _EnvPatch:
    """只打补被测键、退出时仅还原这些键，避免触碰超长环境变量的 Windows 限制。"""

    def __init__(self, **env: str):
        self._env = env
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_EnvPatch":
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc) -> bool:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class _FakeResp:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestLLMDegradation(unittest.TestCase):
    def test_disabled_without_key(self):
        # 显式开，但没有 key（置空以覆盖真实环境可能存在的 key）
        with _EnvPatch(AGENT_LLM="1", AGENT_LLM_API_KEY=""):
            self.assertFalse(llm.is_llm_enabled())
            self.assertIsNone(llm.polish("任意草稿 [1]", "ctx"))

    def test_disabled_when_forced_off(self):
        with _EnvPatch(AGENT_LLM="0", AGENT_LLM_API_KEY="sk-x"):
            self.assertFalse(llm.is_llm_enabled())
            self.assertIsNone(llm.polish("任意草稿 [1]", "ctx"))


class TestLLMCitationSafety(unittest.TestCase):
    def _enabled_env(self) -> dict:
        return {"AGENT_LLM": "1", "AGENT_LLM_API_KEY": "sk-test", "AGENT_LLM_BASE_URL": "https://example/v1"}

    def test_markers_preserved_returns_text(self):
        with _EnvPatch(**self._enabled_env()), \
             patch.object(llm.requests, "post", return_value=_FakeResp("蓝桥杯团队 1–3 人 [1]")):
            out = llm.polish("蓝桥杯团队人数要求：1至3人。[1]", "赛事=蓝桥杯")
        self.assertIsNotNone(out)
        self.assertIn("[1]", out)

    def test_markers_dropped_falls_back(self):
        # 模型把 [1] 弄丢了 → 必须回退 None，引用安全
        with _EnvPatch(**self._enabled_env()), \
             patch.object(llm.requests, "post", return_value=_FakeResp("蓝桥杯团队 1–3 人")):
            out = llm.polish("蓝桥杯团队人数要求：1至3人。[1]", "赛事=蓝桥杯")
        self.assertIsNone(out)

    def test_http_error_falls_back(self):
        with _EnvPatch(**self._enabled_env()), \
             patch.object(llm.requests, "post", side_effect=Exception("network down")):
            out = llm.polish("蓝桥杯团队人数要求：1至3人。[1]", "赛事=蓝桥杯")
        self.assertIsNone(out)

    def test_no_markers_draft_not_validated(self):
        # 原稿无 [n]，模型随便怎么写都不会触发引用校验
        with _EnvPatch(**self._enabled_env()), \
             patch.object(llm.requests, "post", return_value=_FakeResp("润色后的自然文案")):
            out = llm.polish("蓝桥杯团队人数要求：1至3人。", "赛事=蓝桥杯")
        self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()
