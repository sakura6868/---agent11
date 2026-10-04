"""Suggest near-spelled names without selecting a competition or a year."""
import re
import unicodedata

from rapidfuzz.distance import Levenshtein


def suggest_names(question: str, competitions, limit: int = 3) -> list[str]:
    question = unicodedata.normalize("NFKC", question)
    question = re.sub(r"20\d{2}年?", "", question)
    question = re.sub(r"^(?:请问|我想问|我想参加|今年|关于|查询|帮我查|帮我查询)+", "", question)
    fragments = re.findall(r"[\u4e00-\u9fffA-Za-z+]{2,40}?(?:杯|大赛|竞赛)", question)
    found = []
    for name in sorted({comp.competition_name for comp in competitions}):
        normalized = unicodedata.normalize("NFKC", name)
        normalized = re.sub(r"20\d{2}年?", "", normalized)
        keys = [normalized]
        # Short cup brands often occur before the rest of the official title.
        brand = re.match(r"[\u4e00-\u9fffA-Za-z+]{2,12}?杯", normalized)
        if brand:
            keys.append(brand.group())
        if any(key in question for key in keys):
            continue
        for fragment in fragments:
            if any(len(key) >= 3 and Levenshtein.distance(fragment, key) == 1 for key in keys):
                found.append(name)
                break
    return found[:max(0, limit)]
