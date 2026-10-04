"""可选 LLM 润色层（OpenAI 兼容 /chat/completions，零额外依赖）。

设计目标
--------
- **零额外依赖默认可用**：仅用标准库 + 已安装的 `requests`（不依赖 `openai` SDK）。
  未配置 API Key 时，`polish()` / `complete()` 一律返回 None，调用方原样回退到
  确定性模板，不影响任何现有行为。
- **兼容任意 OpenAI 兼容端点**：OpenAI / DeepSeek / 通义千问 / 智谱 / 本地 vLLM
  等，只要暴露 `/chat/completions` 即可（设置 `AGENT_LLM_BASE_URL`）。
- **引用安全（最重要）**：prompt 强约束「不得改写 [n] 引用编号、门控结论、任何数字」；
  润色后做引用标记校验，若 [n] 集合被破坏（丢失/新增），**自动回退原稿**，
  保证「答案里的引用始终来自官方标注、门控结论始终来自本地可信数据」。
- **配置（环境变量）**：
    AGENT_LLM_API_KEY      必填（有它才启用；也可同时设 AGENT_LLM=1 显式开）
    AGENT_LLM_BASE_URL     可选，如 https://api.deepseek.com/v1 ，默认官方地址
    AGENT_LLM_MODEL        可选，默认 gpt-4o-mini
    AGENT_LLM_TEMPERATURE  可选，默认 0.3
    AGENT_LLM_PROVIDER     可选（仅备注/日志用）
    AGENT_LLM              可选：=0 强制关闭；=1 强制开启（需同时有 API Key）

启用判定：`AGENT_LLM != "0"` 且 `AGENT_LLM_API_KEY` 非空。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - requests 为本项目运行依赖，缺失则 LLM 自动关闭
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore

# 引用标记 [n]：用于润色后校验，保证编号不被模型改动。
_MARKER_RE = re.compile(r"\[(\d+)\]")


def _citation_markers(text: str) -> set[int]:
    return {int(m) for m in _MARKER_RE.findall(text or "")}


def is_llm_enabled() -> bool:
    """是否启用了 LLM 润色（未配 key 或 requests 缺失则自动关闭）。"""
    if os.getenv("AGENT_LLM") == "0":
        return False
    if requests is None:
        return False
    return bool(os.getenv("AGENT_LLM_API_KEY"))


def configured_models() -> list[str]:
    default = os.getenv("AGENT_LLM_MODEL") or "gpt-4o-mini"
    optional = (os.getenv("AGENT_LLM_ALLOWED_MODELS") or "").split(",")
    return list(dict.fromkeys([default] + [value.strip() for value in optional if value.strip()]))


def _post_chat(system: str, user: str, model: Optional[str] = None, timeout: float = 20) -> Optional[str]:
    """调用 OpenAI 兼容 /chat/completions；任何异常都返回 None（优雅降级）。

    ``model`` 为可选请求级覆盖：传入时优先于环境变量 ``AGENT_LLM_MODEL``，
    便于前端在同源 key 的模型间（如 qwen-plus / qwen-max / qwen-turbo）切换。
    """
    if not is_llm_enabled():
        return None
    key = os.getenv("AGENT_LLM_API_KEY") or ""
    base_url = (os.getenv("AGENT_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = model or os.getenv("AGENT_LLM_MODEL", "gpt-4o-mini")
    try:
        temperature = float(os.getenv("AGENT_LLM_TEMPERATURE", "0.3"))
    except (TypeError, ValueError):
        temperature = 0.3

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout, allow_redirects=False)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception:  # 网络/鉴权/限流/超时/解析错误一律回退，绝不阻断主链路
        return None


_POLISH_SYSTEM = (
    "你是校园科创导航助手，说话要像一位热心、专业的学长学姐在当面讲解。"
    "请基于【待润色草稿】，用自然、口语化、有交流感的方式重新组织语言，"
    "让回答更像真人在聊天，而不是生硬的条款罗列。必须严格遵守：\n"
    "1. 原样保留草稿里所有引用编号 [1]、[2]…（一个不能少、一个不能多），不要新增或删除；\n"
    "2. 原样保留所有具体事实：日期、截止时间、人数、奖项比例、参赛对象、"
    "以及结论性判断（如『你符合/不符合』『报名已截止』等），不得改写或编造；\n"
    "3. 可以调整句序、加一点自然过渡、让语气更轻松，但不得改变原意；\n"
    "4. 只输出润色后的正文，不要加『以下是润色结果』之类的多余说明。"
)


def polish(draft: str, context: str, model: Optional[str] = None) -> Optional[str]:
    """润色确定性草稿（用于答案 / 组队文案）。

    未启用或调用失败 → 返回 None（调用方回退原稿）。
    若润色结果破坏了引用标记 [n] → 返回 None（同样回退原稿，保证引用安全）。
    ``model`` 可选，请求级覆盖默认模型。
    """
    if not draft or not is_llm_enabled():
        return None
    user = (
        f"【参考事实 / 上下文】\n{context}\n\n"
        f"【待润色草稿】\n{draft}"
    )
    out = _post_chat(_POLISH_SYSTEM, user, model=model)
    if not out:
        return None
    # 引用标记安全校验：仅当原稿含 [n] 时才校验
    orig_markers = _citation_markers(draft)
    if orig_markers and _citation_markers(out) != orig_markers:
        return None
    return out


def complete(system: str, user: str, model: Optional[str] = None) -> Optional[str]:
    """通用问答接口（供未来自由对话 / 复杂意图使用）。"""
    return _post_chat(system, user, model=model)


# ---------------------------------------------------------------------------
# 证据说话 skill（让 LLM 完全依据检索证据作答，防幻觉）
# ---------------------------------------------------------------------------

_SKILL_FILE = Path(__file__).resolve().parent / "skills" / "evidence_speaking.md"
_GROUNDING_SKILL = _SKILL_FILE.read_text(encoding="utf-8") if _SKILL_FILE.exists() else ""


_GENERATE_SYSTEM = (
    "你是「校园科创导航助手」里负责最终作答的学长学姐型参谋，说话自然、口语化、有交流感，"
    "像一个真人辅导员在聊天，而不是生硬罗列条款。\n\n"
    "【证据说话铁律（必须严格遵守）】\n" + _GROUNDING_SKILL + "\n\n"
    "【作答要求】\n"
    "1. 可以解释、对比、给备赛建议、做规划，让回答有「人感」；\n"
    "2. 只要出现关于具体赛事的任一事实（日期、截止、人数、奖项比例、参赛对象、流程、材料、技能等），"
    "必须来自下方【证据】并用 [n] 标注来源编号，一个事实一处引用；\n"
    "3. 若某事实【证据】里没有，绝对不要编造——改为写『这方面建议你直接看官网确认』之类；\n"
    "4. 通用方法论/心态/时间管理类建议（怎么练、怎么组队）不需要引用，可自然展开；\n"
    "5. 只输出回答正文，不要加『以下是回答』之类多余说明。"
)

# chat 意图专用的「话题边界」约束：只聊竞赛方向，不随意发挥
_CHAT_SCOPE = (
    "【方向引导（优先围绕竞赛/科创/成长规划）】\n"
    "你优先围绕「大学生学科竞赛 / 科创活动 / 备赛规划 / 竞赛与升学·就业·综测的关系」"
    "组织回答。如果问题本身比较开放，也尽量从竞赛能力、备赛路径、项目选择、"
    "技能提升、科创规划等角度给出建议，把话题自然拉回竞赛方向。\n"
    "若用户明显跑题（如日常闲聊、情感、娱乐、时政等），可以正常礼貌回应，"
    "但随后应自然地把话题引回竞赛/科创/成长规划，而不是一直顺着无关话题展开。\n"
    "无论何时都不得编造任何具体赛事的事实、日期或数字。"
)


def _build_generate_system(intent: str) -> str:
    """按意图组装 system 提示：chat 额外加「只聊竞赛」话题边界。"""
    if intent == "chat":
        return _GENERATE_SYSTEM + "\n\n" + _CHAT_SCOPE
    return _GENERATE_SYSTEM


def generate_answer(
    question: str,
    evidence_brief: str,
    intent: str = "qa",
    profile_summary: str = "",
    model: Optional[str] = None,
) -> Optional[str]:
    """基于检索证据自由撰写回答（混合模式核心）。

    与 ``polish`` 不同，这里不再是对固定模板改措辞，而是让 LLM 当「参谋」，
    基于【证据】自然组织语言（解释/对比/建议/规划），但所有具体赛事事实必须
    带 [n] 引用、且不得超出证据范围。未启用 LLM 或调用失败返回 None，
    调用方回退确定性模板。
    """
    if not is_llm_enabled():
        return None
    user = f"【用户问题】{question}\n"
    if profile_summary:
        user += f"【用户画像】{profile_summary}\n"
    user += f"【问题类型】{intent}\n"
    user += f"【可用证据】\n{evidence_brief}\n\n"
    user += "请基于上述证据，用自然口语化的方式作答（凡是具体赛事事实务必带 [n] 引用）："
    return _post_chat(_build_generate_system(intent), user, model=model)


if __name__ == "__main__":
    print(f"LLM enabled: {is_llm_enabled()}")
    if is_llm_enabled():
        test = polish(
            "蓝桥杯团队人数要求：1至3人。[1] 报名截止：2026-03-15。",
            "赛事=蓝桥杯",
        )
        print("polish test ->", test)
    else:
        print("未配置 AGENT_LLM_API_KEY（或 requests 缺失），polish 将回退到原稿。")
