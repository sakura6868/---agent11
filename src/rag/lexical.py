"""Offline Chinese retrieval; ranking never changes the original source citation."""
from functools import lru_cache
import re
import unicodedata

import jieba
from rank_bm25 import BM25Plus

_TOKENIZER = jieba.Tokenizer()
_STOPWORDS = frozenset("的 了 吗 呢 啊 是 有 和 与 在 我 我们 请 请问 什么 怎么 如何 可以 是否".split())
_FIELD_TERMS = {
    "registration_deadline": "报名 截止 日期 时间", "registration_deadline_at": "报名 截止 日期 时间",
    "team_min": "团队 人数 队伍 组队 下限", "team_max": "团队 人数 队伍 组队 上限",
    "eligible_students": "参赛 资格 学历 对象", "allowed_majors": "专业 限制 资格",
    "allowed_grades": "年级 限制 资格", "required_materials": "提交 材料 清单",
    "registration_fee": "报名 费用 收费", "competition_end_date": "比赛 结束 日期",
}


@lru_cache(maxsize=2048)
def tokens(text: str) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", text).lower()
    return tuple(word for word in _TOKENIZER.cut_for_search(text)
                 if word not in _STOPWORDS and re.search(r"[\w]", word))


def query_tokens(question: str) -> tuple[str, ...]:
    additions = []
    for triggers, terms in (
        (("几人", "几个人", "多少人", "组队", "人数"), "团队 人数 队伍"),
        (("截止", "最晚", "什么时候报名"), "报名 截止 日期"),
        (("多少钱", "报名费", "收费"), "报名 费用"),
        (("交什么", "交哪些", "材料"), "提交 材料 清单"),
    ):
        if any(trigger in question for trigger in triggers):
            additions.append(terms)
    return tuple(dict.fromkeys(tokens(question + " " + " ".join(additions))))


@lru_cache(maxsize=256)
def _index(documents: tuple[tuple[str, str], ...]):
    corpus = [tokens(text + " " + _FIELD_TERMS.get(field, "")) for text, field in documents]
    # BM25Plus keeps IDF positive even in tiny, per-competition collections.
    return BM25Plus([list(words) for words in corpus]), tuple(frozenset(words) for words in corpus)


def scores(question: str, documents: tuple[tuple[str, str], ...]) -> list[float]:
    words = query_tokens(question)
    if not words or not documents:
        return [0.0] * len(documents)
    # An entirely empty corpus cannot construct a BM25 index.
    if not any(tokens(text + " " + _FIELD_TERMS.get(field, "")) for text, field in documents):
        return [0.0] * len(documents)
    index, corpus = _index(documents)
    raw = index.get_scores(list(words))
    query = set(words)
    # BM25Plus has a smoothing offset; a zero-overlap document is still not a hit.
    return [float(score) if query.intersection(document) else 0.0
            for score, document in zip(raw, corpus)]
