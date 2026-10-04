#!/usr/bin/env python3
"""Reclassify Ground Truth records with the shared official-source rules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from schemas import Competition, DataStatus, TrustedLevel  # noqa: E402
from trust import assess_source_readiness  # noqa: E402

GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "ground_truth" / "samples"


def classify(raw: dict, competition_id: str) -> tuple[str, str, list[str]]:
    candidate = dict(raw)
    candidate.setdefault("competition_id", competition_id)
    candidate["data_status"] = DataStatus.VERIFIED.value
    candidate["trusted_level"] = TrustedLevel.A.value
    try:
        comp = Competition.model_validate(candidate)
    except Exception as exc:
        return DataStatus.UNVERIFIED.value, TrustedLevel.C.value, [f"结构化字段无效：{exc}"]

    assessment = assess_source_readiness(comp)
    if assessment.ready:
        return DataStatus.VERIFIED.value, TrustedLevel.A.value, []
    level = TrustedLevel.B.value if raw.get("official_source_status") == "found" else TrustedLevel.C.value
    return DataStatus.UNVERIFIED.value, level, list(assessment.reasons)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write the derived status back to Ground Truth JSON")
    args = parser.parse_args()

    counts = {"total": 0, "ready": 0, "candidate_b": 0, "candidate_c": 0, "changed": 0}
    for path in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        status, level, reasons = classify(raw, path.stem)
        counts["total"] += 1
        if status == DataStatus.VERIFIED.value:
            counts["ready"] += 1
        else:
            counts[f"candidate_{level.lower()}"] += 1

        changed = raw.get("data_status") != status or raw.get("trusted_level") != level
        if changed:
            counts["changed"] += 1
            print(f"{path.stem}: {raw.get('data_status')}/{raw.get('trusted_level')} -> {status}/{level}")
            if reasons:
                print(f"  {'; '.join(reasons)}")
        if args.write and changed:
            raw["data_status"] = status
            raw["trusted_level"] = level
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    mode = "written" if args.write else "dry-run"
    print(f"{mode}: {json.dumps(counts, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
