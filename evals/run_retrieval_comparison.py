"""Compare the previous character-overlap ranking with offline Chinese BM25Plus.

Small developer-labeled engineering fixture, not independent accuracy validation.
Reads Ground Truth directly; never initializes or writes the user's database.
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rag.lexical import scores
from run_eval import load_competitions


def main():
    fixture = ROOT / "evals/retrieval_cases.json"
    rows = []
    records = {comp.competition_id: comp for comp in load_competitions(ROOT)}
    for case in json.loads(fixture.read_text(encoding="utf-8")):
        blocks = [item for item in records[case["competition_id"]].evidence if item.source_text.strip()]
        query = set(case["question"].lower())
        baseline = [float(sum(char in block.source_text.lower() for char in query)) for block in blocks]
        enhanced = scores(case["question"], tuple((block.source_text, block.field) for block in blocks))
        row = dict(case)
        for label, values in (("character_overlap", baseline), ("jieba_bm25plus", enhanced)):
            ranked = sorted((i for i, score in enumerate(values) if score > 0), key=lambda i: -values[i])
            rank = next((rank for rank, index in enumerate(ranked, 1) if blocks[index].field in case["fields"]), None)
            row[label] = {"top1": rank == 1, "recall_at_3": rank is not None and rank <= 3,
                          "reciprocal_rank": 1 / rank if rank else 0,
                          "retrieved_fields": [blocks[index].field for index in ranked[:3]]}
        rows.append(row)
    summary = {}
    for label in ("character_overlap", "jieba_bm25plus"):
        summary[label] = {"top1_passed": sum(row[label]["top1"] for row in rows), "total": len(rows),
                          "recall_at_3_passed": sum(row[label]["recall_at_3"] for row in rows),
                          "mrr": round(sum(row[label]["reciprocal_rank"] for row in rows) / len(rows), 4)}
    result = {"scope": "20 developer-labeled queries, two competitions; no held-out or real-user validation",
              "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(), "summary": summary, "cases": rows}
    (ROOT / "evals/retrieval_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
