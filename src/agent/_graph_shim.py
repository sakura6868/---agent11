"""LangGraph 兼容降级引擎（仅在本环境无法安装 langgraph 时使用）。

提供与 langgraph.graph 最小兼容的 API：
  - START / END 哨兵
  - StateGraph(state_schema)  + .add_node / .add_edge /
    .add_conditional_edges / .compile()
  - compiled.invoke(state)  -> 按边确定性执行每个 node（接收旧状态、返回更新字典）

注意：本文件是一个**轻量、确定性的有向图执行器**，行为等价于 LangGraph 的
reducer-less 状态传递（每个节点返回 dict，与新状态浅合并）。它不依赖任何
外部服务，保证「离线可运行」。当真实 langgraph 可用时，graph.py 会优先使用
真正的 langgraph，本文件不会被导入。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class _Sentinel:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return self.name


START = _Sentinel("START")
END = _Sentinel("END")


class _CompiledGraph:
    def __init__(self, nodes: dict, edges: list, conditional: dict, start_key: str):
        self._nodes = nodes
        self._edges = edges          # list of (from, to)
        self._conditional = conditional  # key -> (func, mapping)
        self._start = start_key

    def _next(self, key: str, state: dict):
        # 条件边优先
        if key in self._conditional:
            func, mapping = self._conditional[key]
            branch = func(state)
            if branch not in mapping:
                raise ValueError(f"conditional branch '{branch}' 未映射到任何目标节点")
            return mapping[branch]
        # 普通边（取第一个匹配的 from）
        for f, t in self._edges:
            if f == key:
                return t
        return END

    def invoke(self, state: dict) -> dict:
        current = self._start
        visited = 0
        max_steps = 1000
        while current is not END and visited < max_steps:
            visited += 1
            node = self._nodes.get(current)
            if node is None:
                raise ValueError(f"节点 '{current}' 未注册")
            # 节点始终接收最新状态；返回 dict 与新状态浅合并（与 LangGraph 默认行为一致）
            update = node(state)
            if isinstance(update, dict):
                state = {**state, **update}
            elif update is not None:
                state = update
            current = self._next(current, state)
        return state


class StateGraph:
    def __init__(self, state_schema: Any = None):
        self._state_schema = state_schema
        self._nodes: dict[str, Callable] = {}
        self._edges: list[tuple[Any, Any]] = []
        self._conditional: dict[str, tuple[Callable, dict]] = {}

    def add_node(self, key: str, func: Callable) -> "StateGraph":
        self._nodes[key] = func
        return self

    def add_edge(self, f: Any, t: Any) -> "StateGraph":
        self._edges.append((f, t))
        return self

    def add_conditional_edges(
        self, f: Any, func: Callable, mapping: dict
    ) -> "StateGraph":
        # 把 START/END 哨兵统一为字符串名
        norm_map = {}
        for k, v in mapping.items():
            norm_map[k] = v
        self._conditional[f] = (func, norm_map)
        return self

    def compile(self) -> _CompiledGraph:
        # 解析 START -> 首个节点
        start_key = None
        for f, t in self._edges:
            if f is START:
                start_key = t if isinstance(t, str) else getattr(t, "name", None)
        if start_key is None and START in self._conditional:
            start_key = self._conditional[START][1].get(START)
        if start_key is None:
            raise ValueError("未找到 START 起始边")
        # 规范化边里的 START/END 哨兵
        norm_edges = []
        for f, t in self._edges:
            nf = f.name if isinstance(f, _Sentinel) else f
            nt = t.name if isinstance(t, _Sentinel) else t
            if nf != "START" and nt != "END":
                norm_edges.append((nf, nt))
        return _CompiledGraph(self._nodes, norm_edges, self._conditional, start_key)
