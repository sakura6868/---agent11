"""可选联网搜索补充层（Tavily / Serper / DuckDuckGo），零额外依赖（仅 requests）。

设计约束
--------
- 仅在「配置了搜索 key（Tavily/Serper）」或「provider=duckduckgo（免 key）」时启用；
  否则一律返回 ``None``，调用方回退纯本地。
- 联网结果只作为「本地官方证据」的**补充**，绝不与已核验的 ``[n]`` 引用混合；
  由调用方明确标注「🌐 联网信息（仅供参考，以官网为准）」并附来源链接。
- 任何网络 / 鉴权 / 解析异常均返回 ``None``（优雅降级，绝不阻断主链路）。

配置（环境变量）
--------------
    WEB_SEARCH_PROVIDER      tavily | serper | duckduckgo（默认 tavily）
    WEB_SEARCH_API_KEY        Tavily/Serper 必填；duckduckgo 可留空
    WEB_SEARCH_MAX_RESULTS    可选，默认 5

说明
----
- ``duckduckgo`` 走 html.duckduckgo.com 即时搜索，**无需任何 key**，适合演示/内网环境；
  稳定性依赖网络，失败会自动降级到纯本地。
- ``tavily`` / ``serper`` 为专业搜索 API，结果质量与稳定性更好，需自备 key。
"""
from __future__ import annotations

import os
import re

try:  # requests 为本项目运行依赖，缺失则联网自动关闭
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore


def is_web_search_enabled() -> bool:
    """是否启用了联网搜索。

    - ``provider=duckduckgo``：免 key 即可启用（requests 存在即可）。
    - 其它 provider（tavily/serper）：必须配置了 ``WEB_SEARCH_API_KEY``。
    - requests 缺失则一律关闭。
    """
    if requests is None:
        return False
    provider = (os.getenv("WEB_SEARCH_PROVIDER") or "duckduckgo").lower()
    if provider == "duckduckgo":
        return True
    return bool(os.getenv("WEB_SEARCH_API_KEY"))


def web_search(query: str, max_results: int | None = None) -> list[dict] | None:
    """执行联网搜索，返回 ``[{title, url, snippet}]`` 或 ``None``。

    ``None`` 表示未启用 / 检索失败 / 无结果——调用方应回退到纯本地作答。
    """
    if not is_web_search_enabled():
        return None
    provider = (os.getenv("WEB_SEARCH_PROVIDER") or "duckduckgo").lower()
    key = os.getenv("WEB_SEARCH_API_KEY") or ""
    n = int(max_results or os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
    try:
        if provider == "serper":
            return _search_serper(query, key, n)
        if provider == "duckduckgo":
            return _search_duckduckgo(query, n)
        return _search_tavily(query, key, n)
    except Exception:  # 网络/鉴权/限流/超时/解析错误一律回退，绝不阻断主链路
        return None


def _norm_snippet(text: str, limit: int = 220) -> str:
    return " ".join((text or "").split())[:limit]


def _strip_tags(text: str) -> str:
    """去掉 HTML 标签并反转义实体，用于解析搜索结果片段。"""
    import html as _html

    cleaned = re.sub(r"<[^>]+>", "", text or "")
    return _html.unescape(cleaned).strip()


def _search_duckduckgo(query: str, n: int) -> list[dict] | None:
    """免 key 的即时搜索（GET ``duckduckgo.com/html/?q=``）。

    - 实测：沙箱代理放行该端点（POST 到 lite/html 子域会被 TLS 拦截，GET 主域正常）；
      部署到 Render 等直连公网环境同样可用。
    - 引号无关解析：兼容 ``class="result__a"`` / ``class='result-link'``。
    - 链接可能带 ``uddg=`` 重定向，需还原真实 URL。
    - 限流/抖动较常见，故做 3 次重试；任何异常由调用方统一降级为 None。
    """
    import re as _re
    import time as _time
    import urllib.parse as _up

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    url = "https://duckduckgo.com/html/"
    last_err: Exception | None = None
    # 联网补充不能拖住本地可信问答；默认只尝试一次，部署者可通过环境变量
    # WEB_SEARCH_RETRIES=2 显式提高容错。超时后立即回退纯本地证据链。
    retries = max(1, min(3, int(os.getenv("WEB_SEARCH_RETRIES", "1"))))
    timeout_seconds = max(2, min(10, int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "4"))))
    for _attempt in range(retries):
        try:
            resp = requests.get(url, params={"q": query}, headers=headers, timeout=timeout_seconds)  # noqa: S113
            resp.raise_for_status()
            out = _parse_ddg(resp.text, n)
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            last_err = e
        if _attempt + 1 < retries:
            _time.sleep(0.5)
    return None


def _parse_ddg(page: str, n: int) -> list[dict] | None:
    """从 DuckDuckGo 返回页解析结果（兼容 html / lite 两种结构）。"""
    import re as _re
    import urllib.parse as _up

    # 结果链接锚点（html: result__a；lite: result-link；单/双引号均可）
    anchors = _re.findall(
        r'(<a\b[^>]*class=[\'"]result(?:__a|-link)[\'"][^>]*>.*?</a>)',
        page,
        _re.S,
    )
    links: list[tuple[str, str]] = []
    for a in anchors:
        hm = _re.search(r'href=["\']([^"\']+)["\']', a)
        if not hm:
            continue
        href = hm.group(1)
        title = _strip_tags(a)
        links.append((href, title))

    # 摘要（html: result__snippet 包在 <a>；lite: result-snippet 包在 <td>）
    snips = _re.findall(
        r'class=[\'"]result[-_]?_?snippet[\'"][^>]*>(.*?)</(?:a|td|div)>',
        page,
        _re.S,
    )
    snippets = [_strip_tags(s) for s in snips]

    out: list[dict] = []
    for i, (href, title) in enumerate(links[:n]):
        real = href
        if "/l/?uddg=" in href:
            try:
                real = _up.parse_qs(_up.urlparse(href).query).get("uddg", [href])[0]
            except Exception:  # noqa: BLE001
                real = href
        snip = snippets[i] if i < len(snippets) else ""
        out.append(
            {
                "title": title,
                "url": real,
                "snippet": _norm_snippet(snip),
            }
        )
    return out or None


def _search_tavily(query: str, key: str, n: int) -> list[dict] | None:
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": key,
        "query": query,
        "max_results": n,
        "search_depth": "basic",
    }
    resp = requests.post(url, json=payload, timeout=15)  # noqa: S113
    resp.raise_for_status()
    data = resp.json()
    out = []
    for it in (data.get("results") or [])[:n]:
        title = (it.get("title") or "").strip()
        link = (it.get("url") or "").strip()
        if not link:
            continue
        out.append(
            {
                "title": title,
                "url": link,
                "snippet": _norm_snippet(it.get("content") or ""),
            }
        )
    return out or None


def _search_serper(query: str, key: str, n: int) -> list[dict] | None:
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    resp = requests.get(url, params={"q": query, "num": n}, headers=headers, timeout=15)  # noqa: S113
    resp.raise_for_status()
    data = resp.json()
    out = []
    for it in (data.get("organic") or [])[:n]:
        title = (it.get("title") or "").strip()
        link = (it.get("link") or "").strip()
        if not link:
            continue
        out.append(
            {
                "title": title,
                "url": link,
                "snippet": _norm_snippet(it.get("snippet") or ""),
            }
        )
    return out or None


if __name__ == "__main__":
    print(f"web search enabled: {is_web_search_enabled()}")
    if is_web_search_enabled():
        print(web_search("蓝桥杯 2026 报名通知"))
    else:
        print("未配置 WEB_SEARCH_API_KEY，联网搜索将保持关闭（纯本地作答）。")
