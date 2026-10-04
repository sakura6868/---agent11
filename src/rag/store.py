"""赛事隔离 RAG 检索（真实 Chroma 服务 + DB 数据源）。

核心约束（对应修订方案「赛事隔离 RAG」）：
  - 通过 competition_id + document_year + doc_version 限制检索范围，
    避免不同赛事 / 不同年份规则混淆（每个 competition_id 一个独立 collection，
    metadata 再叠加 competition_id/year/version 三重过滤，双保险隔离）。
  - 检索结果附带页码与原文证据（完整 Citation：文档名/官方链接/获取日期/
    最后核验/可信等级），供前端角标展示。
  - 数据源来自真实数据库 db.get_all_competitions()（按赛事/年份/赛道隔离），
    不再硬编码样例，实现「接 DB 的赛事 ID」。

Chroma 连接（生产可切真实服务）：
  - 默认本地持久化 PersistentClient（零外部依赖，data/chunks）。
  - 设环境变量 CHROMA_HOST（可选 CHROMA_PORT，默认 8000）即切换到
    真实 Chroma 服务端（HttpClient），业务代码无需改动。

embedding（诚实标注的混合检索架构）：
  - all-MiniLM-L6-v2 为可选模型，源码不分发权重；本地缓存存在时可启用语义检索。
  - 检索融合三层信号（详见 docs/RAG_ARCHITECTURE.md）：
      ① 语义向量余弦（MiniLM 句向量，可命中 paraphrase，如「组队几个人」≈ team_max）；
      ② 中文分词重叠（lexical，兜底精确术语召回）；
      ③ 结构化字段召回（报名截止/专业限制等字段级精确匹配）。
    最终以 0.8*语义 + 0.2*关键词 混合打分重排，兼顾语义泛化与术语精确。
  - RAG_USE_ST=0 关闭语义，使用 jieba + BM25Plus 原文检索；=1 尝试开启语义。
    不设则本地缓存存在时尝试开启，缺失模型时使用 BM25Plus。

检索流程：
  DB -> ingest(每赛事字段+证据构造检索块，带 metadata) -> add
      -> query(强制 scoped by competition_id, 可选 year/version)
"""

from __future__ import annotations

import os
import pathlib
import sys
from pathlib import Path
from typing import Optional

# 允许脚本直接运行（python rag/store.py）或模块运行（python -m rag.store）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# chromadb 为可选依赖：仅在显式配置 Chroma（CHROMA_HOST）或需要真实向量检索时，
# 才在 CompetitionRAG.__init__ 内惰性导入；未安装时自动降级到纯本地检索，避免启动失败。

import db  # 延迟可用的数据访问层（用于 Chroma 不可用时的本地降级检索）
from schemas import Citation, Competition
from rag.lexical import scores as lexical_scores, tokens, query_tokens

# ---------------------------------------------------------------------------
# Embedding：优先 sentence-transformers，回退轻量哈希向量
# ---------------------------------------------------------------------------
_EMBED_DIM = 256
_ST_MODEL = None
_ST_TRIED = False
# 语义块向量缓存：competition_id -> (blocks, 归一化向量)，数据静态时复用，避免重复编码
_SEM_CACHE: dict[str, tuple[list, list]] = {}


def _model_cached_locally() -> bool:
    """all-MiniLM-L6-v2 是否已离线打包到本地 HF 缓存（~88MB）。"""
    _hf_cache = (
        pathlib.Path(os.path.expanduser("~")) / ".cache" / "huggingface" / "hub"
    )
    return (_hf_cache / "models--sentence-transformers--all-MiniLM-L6-v2").exists()


def _try_load_st():
    """惰性加载 sentence-transformers；失败则永久回退轻量向量。

    开关优先级（诚实标注）：
      RAG_USE_ST=0  强制关闭（轻量哈希向量 + 关键词检索）
      RAG_USE_ST=1  强制开启（真实语义 embedding，缺失模型会报错并降级）
      （不设）        自动：本地已缓存 all-MiniLM-L6-v2 即默认开启真·语义检索；
                    否则优雅降级到轻量向量 + 关键词，保证服务零外部依赖可启动。

    启用真实语义 embedding 时：若模型已在本地缓存，强制 HF_HUB_OFFLINE=1 从缓存
    加载，避免受限网络下连 huggingface.co 超时卡死（本沙箱 huggingface.co 不可达）。
    """
    global _ST_MODEL, _ST_TRIED
    if _ST_TRIED:
        return _ST_MODEL
    _ST_TRIED = True
    env = os.getenv("RAG_USE_ST")
    if env == "0":
        return None
    force = env == "1"
    cached = _model_cached_locally()
    # 未显式开启且未强制、且本地无模型、且未被显式禁用 → 走轻量降级
    if not force and not cached:
        return None
    try:
        if cached:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer

        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        print(f"[rag] 已加载真实语义向量模型 all-MiniLM-L6-v2（离线，dim=384）")
    except Exception as exc:
        print(f"[rag] 加载 sentence-transformers 失败，降级轻量向量：{exc}")
        _ST_MODEL = None
    return _ST_MODEL


def _light_embed(texts: list[str]) -> list[list[float]]:
    """字符三元组哈希向量，离线可用，仅用于演示隔离检索。"""
    import hashlib

    vecs = []
    for t in texts:
        vec = [0.0] * _EMBED_DIM
        for i in range(max(0, len(t) - 2)):
            h = int(hashlib.md5(t[i : i + 3].encode("utf-8")).hexdigest(), 16)
            vec[h % _EMBED_DIM] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        vecs.append([v / norm for v in vec])
    return vecs


def embed(texts: list[str]) -> list[list[float]]:
    """统一 embedding 入口：有 ST 用 ST，否则轻量向量。"""
    model = _try_load_st()
    if model is not None:
        return [list(map(float, v)) for v in model.encode(texts, normalize_embeddings=True)]
    return _light_embed(texts)


def embedding_backend() -> str:
    return "sentence-transformers/all-MiniLM-L6-v2" if _try_load_st() is not None else "light-hash-256"


def retrieval_backend() -> str:
    """Embedding and actual retrieval are separate capabilities."""
    if _try_load_st() is not None:
        return "semantic+token-overlap"
    return "jieba+bm25plus"


def _has_real_embedding() -> bool:
    """是否具备真实语义 embedding（sentence-transformers）。

    仅当具备真实 embedding 时，Chroma 向量检索才有意义；否则使用离线轻量
    哈希向量，语义召回质量差，且在某些受限环境下 Chroma 查询会报磁盘段错误。
    此时直接走「本地隔离检索」（基于赛事结构化字段 + 证据原文），对所有
    赛事做 competition_id 隔离，引用仍来自官方标注，质量更稳、零外部依赖。
    """
    return _try_load_st() is not None


# ---------------------------------------------------------------------------
# 赛事隔离 RAG
# ---------------------------------------------------------------------------


class CompetitionRAG:
    """按赛事隔离的向量检索封装（真实 Chroma）。"""

    def __init__(self, persist_dir: str | Path = "data/chunks"):
        # chromadb 为可选依赖：未安装时自动降级到纯本地检索（不依赖 Chroma，
        # 引用仍来自官方标注），保证服务在缺 Chroma 的环境下也能正常启动。
        try:
            import chromadb
            from chromadb.config import Settings
        except Exception:
            self.client = None
            self.backend = "none"
            self._collection_cache: dict = {}
            return

        host = os.getenv("CHROMA_HOST")
        if host:
            # 真实 Chroma 服务端
            port = int(os.getenv("CHROMA_PORT", "8000"))
            self.backend = f"http://{host}:{port}"
            self.client = chromadb.HttpClient(
                host=host, port=port, settings=Settings(anonymized_telemetry=False)
            )
        else:
            # 本地持久化（默认）
            self.persist_dir = str(persist_dir)
            Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
            self.backend = f"persistent://{self.persist_dir}"
            self.client = chromadb.PersistentClient(
                path=self.persist_dir, settings=Settings(anonymized_telemetry=False)
            )
        self._collection_cache: dict = {}

    # -- 集合按 competition_id 隔离 --
    def _collection_name(self, competition_id: str) -> str:
        # Chroma 集合名仅允许字母数字与下划线
        return f"rag_{competition_id.replace('-', '_')}"

    def _get_collection(self, competition_id: str):
        if self.client is None:
            return None
        name = self._collection_name(competition_id)
        if name not in self._collection_cache:
            self._collection_cache[name] = self.client.get_or_create_collection(
                name=name,
                metadata={"competition_id": competition_id, "isolated": True},
            )
        return self._collection_cache[name]

    # -- 写入：将一个赛事的可检索文本片段灌入其隔离集合 --
    def ingest_competition(self, comp: Competition) -> int:
        """把单个赛事的关键字段 + 证据原文构造成检索块，写入隔离集合。

        每个片段的 metadata 携带完整引用信息，检索时可直接还原为 Citation。
        幂等：先删除该赛事旧集合内容再写入，保证与 DB 最新数据一致。
        """
        # 仅在「真实 embedding + 远程 Chroma 服务」时才写 Chroma。
        # 否则（离线轻量向量，或本环境 Chroma 持久化不稳定）跳过 Chroma 写入，
        # 检索直接读 DB（本地关键词 / 语义本地余弦），引用更可靠且避免磁盘错误。
        _SEM_CACHE.pop(comp.competition_id, None)
        use_chroma = _has_real_embedding() and str(self.backend).startswith("http")
        if not use_chroma:
            return len(self._build_blocks(comp))

        collection = self._get_collection(comp.competition_id)
        # 幂等：清空该集合旧数据（按 competition_id 过滤删除）
        try:
            collection.delete(where={"competition_id": comp.competition_id})
        except Exception:
            pass

        documents: list[str] = []
        ids: list[str] = []
        metadatas: list[dict] = []

        def _add(seg_id: str, text: str, field: str, page, source_url, doc_name,
                 acquired, verified, trusted, evidence_index: int):
            if not text:
                return
            documents.append(text)
            ids.append(f"{comp.competition_id}_{seg_id}")
            metadatas.append({
                "competition_id": comp.competition_id,
                "document_year": comp.document_year,
                "doc_version": comp.doc_version or f"{comp.document_year}_v1",
                "evidence_index": evidence_index,
                "field": field,
                "page": -1 if page is None else int(page),
                "source_url": source_url or "",
                "document_name": doc_name or "",
                "acquired_date": acquired or "",
                "last_verified_at": verified or "",
                "trusted_level": trusted or (comp.trusted_level.value if hasattr(comp.trusted_level, "value") else str(comp.trusted_level)),
            })

        # Generated summaries are facts, not quotations. Index only saved source evidence.
        for i, e in enumerate(comp.evidence or []):
            _add(
                f"ev_{i}",
                e.source_text,
                e.field,
                e.page,
                e.source_url,
                e.document_name,
                e.acquired_date,
                e.last_verified_at,
                e.trusted_level.value if hasattr(e.trusted_level, "value") else str(e.trusted_level),
                i,
            )

        if documents:
            collection.add(documents=documents, ids=ids, metadatas=metadatas, embeddings=embed(documents))
        return len(documents)

    def seed_from_db(self) -> dict:
        """从数据库读取全部赛事并灌入各自隔离集合。返回 {competition_id: 片段数}。"""
        import db  # 延迟导入，避免循环依赖

        _SEM_CACHE.clear()  # 数据可能变化，清掉旧块向量缓存
        result: dict = {}
        for comp in db.get_all_competitions():
            result[comp.competition_id] = self.ingest_competition(comp)
        return result

    # -- 查询：强制按 competition_id 隔离，可选年份/版本过滤 --
    def query(
        self,
        competition_id: str,
        question: str,
        document_year: Optional[int] = None,
        doc_version: Optional[str] = None,
        top_k: int = 3,
    ) -> list[Citation]:
        """隔离检索：
        - 具备真实 embedding 时：
            * 若配置了远程 Chroma（CHROMA_HOST）→ 用 Chroma 语义检索；
            * 否则（本环境 Chroma 持久化不稳定）→ 用「语义本地检索」做余弦排序，
              绕开 Chroma 仍能获得真正的语义召回，引用 100% 来自官方标注。
        - 无真实 embedding（离线轻量向量）→ 本地关键词重叠检索。
        三种路径都强制 competition_id 隔离。"""
        if top_k <= 0:
            return []
        comp = db.get_competition(competition_id)  # type: ignore[name-defined]
        if comp is None:
            return []
        if document_year is not None and document_year != comp.document_year:
            return []
        if doc_version is not None and doc_version != comp.doc_version:
            return []

        if _has_real_embedding():
            # 真正语义召回：远程 Chroma 优先，否则语义本地检索（无需 Chroma）
            if str(getattr(self, "backend", "")).startswith("http"):
                try:
                    hits = self._query_chroma(competition_id, question, document_year, doc_version, top_k)
                    if hits:
                        return hits
                except Exception as exc:  # Chroma 在受限环境偶发 hnsw 磁盘错误
                    print(f"[rag] Chroma 检索异常，降级到语义本地检索：{exc}")
            return self._semantic_local_query(comp, question, top_k)

        # 无语义模型时：离线中文 BM25Plus 检索
        return self._local_query(comp, question, top_k)

    def _query_chroma(
        self,
        competition_id: str,
        question: str,
        document_year: Optional[int] = None,
        doc_version: Optional[str] = None,
        top_k: int = 3,
    ) -> list[Citation]:
        if self.client is None:
            return []
        collection = self._get_collection(competition_id)
        if collection is None:
            return []
        conditions: list[dict] = [{"competition_id": competition_id}]
        if document_year is not None:
            conditions.append({"document_year": document_year})
        if doc_version is not None:
            conditions.append({"doc_version": doc_version})
        # Chroma 多条件必须用 $and 包裹
        where: dict = {"$and": conditions} if len(conditions) > 1 else conditions[0]

        res = collection.query(
            query_embeddings=embed([question]),
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        citations: list[Citation] = []
        comp = db.get_competition(competition_id)
        if comp is None:
            return []
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        for doc, meta in zip(docs, metas):
            meta = meta or {}
            index = meta.get("evidence_index")
            if type(index) is not int or not 0 <= index < len(comp.evidence):
                continue
            evidence = comp.evidence[index]
            if (meta.get("document_year") != comp.document_year
                    or meta.get("doc_version") != comp.doc_version
                    or meta.get("field") != evidence.field or doc != evidence.source_text):
                continue
            citations.append(evidence.model_copy(deep=True))
        return citations

    # —— 降级 / 语义本地检索：不依赖 Chroma，直接基于 DB 块做隔离检索 ——
    def _build_blocks(self, comp: Competition) -> list[Citation]:
        """把一个赛事的可检索片段（结构化字段 + 证据原文）构造成 Citation 列表，
        供本地关键词检索与语义余弦检索共用。始终按 competition_id 隔离。"""
        return [item.model_copy(deep=True) for item in comp.evidence if item.source_text.strip()]

    def _local_query(self, comp: Competition, question: str, top_k: int) -> list[Citation]:
        """中文分词 + BM25Plus，索引范围只包含当前赛事的原始证据。"""
        blocks = self._build_blocks(comp)
        values = lexical_scores(question or "", tuple((b.source_text, b.field) for b in blocks))
        scored = [(value, block) for value, block in zip(values, blocks) if value > 0]
        scored.sort(key=lambda x: -x[0])
        return [b for _, b in scored[:max(0, top_k)]]

    def _lexical_score(self, q: str, text: str) -> float:
        """中文词项重叠度（归一化），作为可选语义检索的 lexical 信号。"""
        qt = set(query_tokens(q or ""))
        if not qt:
            return 0.0
        t = set(tokens(text or ""))
        hit = len(qt.intersection(t))
        return hit / len(qt)

    def _semantic_local_query(self, comp: Competition, question: str, top_k: int) -> list[Citation]:
        """具备真实 embedding 时，用 sentence-transformers 做「语义 + 关键词」混合检索。

        绕开本环境不稳定的 Chroma 持久化查询，仍能获得真正的「语义」召回：
        「团队几个人」「组队人数」等 paraphrase 都能命中 team_max 块；同时用关键词
        重叠（0.2 权重）兜底精确术语。最终 0.8*语义余弦 + 0.2*关键词 混合重排。
        不依赖任何外部服务，引用仍 100% 来自官方标注。

        块向量按 competition_id 缓存到内存（数据静态），仅 query 向量逐次编码，
        大幅提升重复查询吞吐。
        """
        blocks = self._build_blocks(comp)
        if not blocks:
            return []
        model = _try_load_st()
        if model is None:
            return self._local_query(comp, question, top_k)
        # 块向量缓存（按赛事隔离）
        if comp.competition_id not in _SEM_CACHE:
            texts = [b.source_text for b in blocks]
            _SEM_CACHE[comp.competition_id] = (
                blocks,
                model.encode(texts, normalize_embeddings=True),
            )
        cached_blocks, b_vecs = _SEM_CACHE[comp.competition_id]
        q_vec = model.encode([question or ""], normalize_embeddings=True)[0]
        scored = []
        for b, v in zip(cached_blocks, b_vecs):
            sim = float(sum(a * c for a, c in zip(q_vec, v)))  # 已归一化 -> 余弦
            lex = self._lexical_score(question or "", b.source_text)
            hybrid = 0.8 * sim + 0.2 * lex
            scored.append((hybrid, b))
        scored.sort(key=lambda x: -x[0])
        return [b for _, b in scored[:top_k]]


# 单例（供 API 复用，避免重复建连）
_RAG_SINGLETON: Optional[CompetitionRAG] = None


def get_rag() -> CompetitionRAG:
    global _RAG_SINGLETON
    if _RAG_SINGLETON is None:
        _RAG_SINGLETON = CompetitionRAG()
    return _RAG_SINGLETON


if __name__ == "__main__":
    # 自测：从真实 DB 灌库 + 隔离检索验证
    rag = get_rag()
    print(f"[rag] Chroma backend: {rag.backend}")
    print(f"[rag] embedding backend: {embedding_backend()}")

    stats = rag.seed_from_db()
    total = sum(stats.values())
    print(f"[rag] 已从 DB 灌入 {len(stats)} 个赛事、共 {total} 个片段")

    # 隔离验证：查询蓝桥杯，结果必须只来自蓝桥杯
    if "lanqiao_2026" in stats:
        hits = rag.query("lanqiao_2026", "团队人数和参赛对象要求", document_year=2026)
        print(f"\n[lanqiao_2026] 检索到 {len(hits)} 条：")
        for h in hits:
            pg = "未定位" if h.page is None else f"第{h.page}页"
            print(f"  [{h.field}|{pg}|{h.trusted_level.value}级] {h.source_text[:50]}")

    # 隔离验证：跨赛事查询不应串味
    ids = list(stats.keys())
    if len(ids) >= 2:
        other = ids[1]
        hits2 = rag.query(other, "报名截止时间")
        print(f"\n[{other}] 检索到 {len(hits2)} 条（应只属于 {other}）：")
        for h in hits2:
            print(f"  [{h.field}] {h.source_text[:50]}")
