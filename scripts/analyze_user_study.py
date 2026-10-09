"""Recalculate public aggregates. This cannot verify unprovided student records."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    """Hash public text with LF normalization so Windows checkouts remain valid."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def percentage(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def verification_supported(report: dict) -> bool:
    """Check required declarations; a human must verify their underlying evidence."""
    provenance = report.get("provenance", {})
    binding = report.get("version_binding", {})
    verification = report.get("verification", {})
    fingerprint = provenance.get("private_source_sha256")
    dataset = binding.get("tested_dataset_sha256")
    return bool(
        report.get("evidence_status") == "independently_verified"
        and verification.get("independent_validation_complete") is True
        and report.get("verified") is True
        and provenance.get("independent_observation_verified") is True
        and provenance.get("consent_artifacts_verified") is True
        and provenance.get("session_dates_verified") is True
        and provenance.get("raw_workbook_recalculated_by_current_reviewer") is True
        and binding.get("binding_verified") is True
        and binding.get("tested_application_commit")
        and isinstance(fingerprint, str) and len(fingerprint) == 64
        and all(char in "0123456789abcdef" for char in fingerprint)
        and isinstance(dataset, str) and len(dataset) == 64
        and all(char in "0123456789abcdef" for char in dataset)
        and not verification.get("pending")
    )


def validate_aggregates(report: dict, audit: dict) -> list[str]:
    errors: list[str] = []

    def count(value, name: str) -> bool:
        if type(value) is not int or value < 0:
            errors.append(f"{name}: expected a nonnegative integer")
            return False
        return True

    def bounded_counts(group: dict, keys: tuple[str, ...], denominator: int, name: str):
        for key in keys:
            value = group.get(key)
            if count(value, f"{name}.{key}") and value > denominator:
                errors.append(f"{name}.{key}: exceeds denominator")

    participants = report.get("participants", {})
    for key in ("recorded", "included", "completed", "withdrew", "not_started", "complete_questionnaires"):
        count(participants.get(key), f"participants.{key}")
    if not errors:
        if participants["completed"] + participants["withdrew"] + participants["not_started"] != participants["recorded"]:
            errors.append("participant status totals differ from recorded count")
        if participants["included"] > participants["recorded"]:
            errors.append("included participants exceed recorded participants")
        if participants["complete_questionnaires"] > participants["included"]:
            errors.append("complete questionnaires exceed included participants")

    tasks = report.get("tasks", [])
    seen: set[str] = set()
    attempts_sum = 0
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or task_id in seen:
            errors.append("invalid or duplicate task ID")
        seen.add(str(task_id))
        keys = ("attempts", "independent_successes", "assisted_successes", "total_successes", "not_successful")
        if not all([count(task.get(key), f"{task_id}.{key}") for key in keys]):
            continue
        attempts_sum += task["attempts"]
        successes = task["independent_successes"] + task["assisted_successes"]
        if successes != task["total_successes"] or successes + task["not_successful"] != task["attempts"]:
            errors.append(f"{task_id}: task outcome totals are inconsistent")
        for field, numerator in (("success_percent", successes), ("independent_success_percent", task["independent_successes"])):
            if field in task and task[field] != percentage(numerator, task["attempts"]):
                errors.append(f"{task_id}.{field}: incorrect percentage")
    if len(tasks) != 10:
        errors.append("expected ten documented task definitions")

    rows = report.get("task_rows", {})
    for key in ("baseline_before_selection", "retained_raw", "included", "included_started_attempts", "removed"):
        count(rows.get(key), f"task_rows.{key}")
    if all(type(rows.get(key)) is int and rows[key] >= 0 for key in rows):
        if attempts_sum != rows.get("included_started_attempts"):
            errors.append("sum of task attempts differs from included started attempts")
        if rows.get("included_started_attempts", 0) > rows.get("included", 0) or rows.get("included", 0) > rows.get("retained_raw", 0):
            errors.append("task row denominators are inconsistent")
        if rows.get("baseline_before_selection", 0) - rows.get("removed", 0) != rows.get("retained_raw"):
            errors.append("selection row totals are inconsistent")
    for source, target in (("baseline_before_selection", "baseline_task_rows"), ("retained_raw", "retained_task_rows"), ("removed", "removed_task_rows")):
        if rows.get(source) != audit.get(target):
            errors.append(f"selection audit mismatch: {target}")
    removals = audit.get("removed_by_classification", [])
    if all([count(item.get("count"), "removed_by_classification.count") for item in removals]):
        if sum(item["count"] for item in removals) != rows.get("removed"):
            errors.append("removed classification counts do not sum to removed rows")
    if audit.get("success_observations_added") != 0:
        errors.append("selection audit must not add success observations")

    ratings = report.get("recommendation_ratings", {})
    if count(ratings.get("ratings"), "recommendation_ratings.ratings"):
        bounded_counts(ratings, ("rating_at_least_4", "fully_verified_entries"), ratings["ratings"], "recommendation_ratings")
    if count(ratings.get("participants"), "recommendation_ratings.participants") and type(participants.get("included")) is int:
        if ratings["participants"] > participants["included"] or ratings["participants"] > ratings.get("ratings", 0):
            errors.append("recommendation rating participant denominator is inconsistent")
    mean = ratings.get("mean_relevance_1_to_5")
    if type(mean) not in (int, float) or not math.isfinite(mean) or not 1 <= mean <= 5:
        errors.append("recommendation relevance mean is outside the scale")

    questionnaire = report.get("questionnaire", {})
    responses = questionnaire.get("complete_responses")
    if count(responses, "questionnaire.complete_responses"):
        if responses != participants.get("complete_questionnaires"):
            errors.append("questionnaire denominator mismatch")
        for item in questionnaire.get("items", []):
            bounded_counts(item, ("rating_at_least_4",), responses, "questionnaire.item")
            mean = item.get("mean")
            if type(mean) not in (int, float) or not math.isfinite(mean) or not 1 <= mean <= 5:
                errors.append("questionnaire mean is outside the scale")

    timing = report.get("paired_timing", {})
    timing_keys = ("recorded_pairs", "included_pairs", "system_slower_pairs")
    if all([count(timing.get(key), f"paired_timing.{key}") for key in timing_keys]):
        if timing["system_slower_pairs"] > timing["included_pairs"] or timing["included_pairs"] > timing["recorded_pairs"]:
            errors.append("paired timing denominators are inconsistent")
    means_valid = True
    for key in ("manual_mean_seconds", "system_mean_seconds", "mean_seconds_saved", "mean_pairwise_saving_percent"):
        value = timing.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            errors.append(f"paired_timing.{key}: invalid number")
            means_valid = False
        elif key in ("manual_mean_seconds", "system_mean_seconds") and value <= 0:
            errors.append(f"paired_timing.{key}: must be positive")
    if means_valid and abs(timing["manual_mean_seconds"] - timing["system_mean_seconds"] - timing["mean_seconds_saved"]) > 0.151:
        errors.append("paired timing rounded means are inconsistent")

    feedback = report.get("feedback", {})
    feedback_keys = ("valid_records", "recommendation_records", "portfolio_records")
    if all([count(feedback.get(key), f"feedback.{key}") for key in feedback_keys]):
        if feedback["recommendation_records"] + feedback["portfolio_records"] > feedback["valid_records"]:
            errors.append("feedback category counts exceed total")

    claims_verified = report.get("verified") is True or report.get("verification", {}).get("real_user_validation_flag") is True
    if claims_verified and not verification_supported(report):
        errors.append("independent validation claim lacks required provenance declarations")
    return errors


def calculate(report: dict, audit: dict) -> dict:
    errors = validate_aggregates(report, audit)
    if errors:
        raise ValueError("\n".join(errors))
    result = {
        "evidence_status": report["evidence_status"],
        "independent_validation_supported": verification_supported(report),
        "tasks": [dict(id=task["id"], success_percent=percentage(task["total_successes"], task["attempts"]),
                       independent_success_percent=percentage(task["independent_successes"], task["attempts"]))
                  for task in report["tasks"]],
        "selection_sensitivity": [],
        "limitations": "Recalculation uses team-reported aggregates, not raw workbook rows. Individual consent, dates and observations are not verified.",
    }
    for task_id in ("T04", "T05"):
        task = next(task for task in report["tasks"] if task["id"] == task_id)
        removed = sum(item["count"] for item in audit["removed_by_classification"] if item["task_id"] == task_id)
        result["selection_sensitivity"].append(dict(
            task_id=task_id, retained_attempts=task["attempts"], removed_unsuccessful_attempts=removed,
            baseline_attempts=task["attempts"] + removed, successes=task["total_successes"],
            baseline_success_percent=percentage(task["total_successes"], task["attempts"] + removed),
            retained_success_percent=percentage(task["total_successes"], task["attempts"])))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "evals/user_study_results.json")
    parser.add_argument("--audit", type=Path, default=ROOT / "evidence/user_study/selection_audit.json")
    args = parser.parse_args()
    try:
        result = calculate(read_json(args.report), read_json(args.audit))
        result["input_sha256"] = {"report": sha256(args.report), "selection_audit": sha256(args.audit)}
        result["input_hash_mode"] = "text-lf-normalized-sha256"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"FAIL {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
