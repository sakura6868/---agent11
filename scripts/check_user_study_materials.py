"""Validate public user-study files without claiming student authenticity."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.analyze_user_study import calculate, read_json, sha256, verification_supported

MANIFEST_PATH = "evidence/user_study/manifest.json"
EVIDENCE_FILES = (
    "evals/user_study_results.json",
    "evidence/user_study/selection_audit.json",
    "evidence/user_study/README.md",
    "docs/USER_STUDY.md",
    "docs/USER_STUDY_QA.md",
    "docs/DATA_PROVENANCE.md",
    "submission/06_用户试用与效果说明.md",
    "scripts/analyze_user_study.py",
    "scripts/check_user_study_materials.py",
    "scripts/test_user_study_evidence.py",
)


def check_files(root: Path) -> list[str]:
    return [f"missing file: {name}" for name in EVIDENCE_FILES if not (root / name).is_file()]


def check_links(root: Path, *, pending_manifest: bool = False) -> list[str]:
    errors = []
    files = [root / "README.md", root / "SUBMISSION.md", root / "参赛作品说明.md"]
    for directory in ("docs", "submission", "evidence"):
        files.extend((root / directory).rglob("*.md"))
    for path in files:
        if not path.is_file():
            continue
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8-sig")):
            target = target.strip().strip("<>")
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            if pending_manifest and (path.parent / target).resolve() == (root / MANIFEST_PATH).resolve():
                continue
            if target and not (path.parent / target).exists():
                errors.append(f"broken link: {path.relative_to(root)} -> {target}")
    return errors


def validate_manifest(root: Path) -> list[str]:
    path = root / MANIFEST_PATH
    if not path.is_file():
        return [f"missing file: {MANIFEST_PATH}"]
    try:
        manifest = read_json(path)
    except (OSError, ValueError) as exc:
        return [f"invalid manifest: {exc}"]
    errors = []
    actual = manifest.get("files", {})
    if set(actual) != set(EVIDENCE_FILES):
        errors.append("manifest file list differs from required evidence file list")
    for name in EVIDENCE_FILES:
        file_path = root / name
        fingerprint = actual.get(name)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            errors.append(f"invalid or missing SHA-256: {name}")
        elif not file_path.is_file() or sha256(file_path) != fingerprint:
            errors.append(f"fingerprint mismatch: {name}")
    if manifest.get("authenticity_verified") is not False:
        errors.append("material manifest must not certify student authenticity")
    if manifest.get("hash_mode") != "text-lf-normalized-sha256":
        errors.append("manifest hash mode must be text-lf-normalized-sha256")
    return errors


def write_manifest(root: Path, document_date: str) -> None:
    date.fromisoformat(document_date)
    manifest = {
        "schema_version": "1.0",
        "document_date": document_date,
        "scope": "public aggregate, method, audit and validation files; excludes manifest itself",
        "authenticity_verified": False,
        "hash_mode": "text-lf-normalized-sha256",
        "limitations": "Hashes establish file identity only. No raw workbook, student identity or consent evidence was verified.",
        "files": {name: sha256(root / name) for name in EVIDENCE_FILES},
    }
    path = root / MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_materials(root: Path, *, verify_hashes: bool = True) -> list[str]:
    errors = check_files(root)
    errors.extend(check_links(root, pending_manifest=not verify_hashes))
    report_path = root / "evals/user_study_results.json"
    audit_path = root / "evidence/user_study/selection_audit.json"
    if report_path.is_file() and audit_path.is_file():
        try:
            report = read_json(report_path)
            computed = calculate(report, read_json(audit_path))
            snapshot = read_json(root / "evals/submission_snapshot.json")
            if snapshot.get("real_user_validation") != verification_supported(report):
                errors.append("submission snapshot and supported user validation state differ")
            if snapshot.get("user_study", {}).get("aggregate_sha256") != sha256(report_path):
                errors.append("submission snapshot user-study fingerprint differs")
            summary = snapshot.get("user_study", {})
            expected = {
                "population": report["participants"]["population"],
                "status": report["evidence_status"],
                "included_participants": report["participants"]["included"],
                "complete_questionnaires": report["participants"]["complete_questionnaires"],
                "selection_sensitivity": computed["selection_sensitivity"],
                "aggregate_hash_mode": "text-lf-normalized-sha256",
            }
            for key, value in expected.items():
                if summary.get(key) != value:
                    errors.append(f"submission snapshot user-study field differs: {key}")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"aggregate/snapshot check failed: {exc}")
    if verify_hashes:
        errors.extend(validate_manifest(root))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--document-date", default="2026-10-10")
    args = parser.parse_args()
    errors = check_materials(ROOT, verify_hashes=not args.write_manifest)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    if args.write_manifest:
        write_manifest(ROOT, args.document_date)
        errors = validate_manifest(ROOT)
        if errors:
            for error in errors:
                print(f"FAIL {error}")
            return 1
    print("PASS aggregate arithmetic, selection counts, local links, snapshot state and SHA-256 manifest")
    print("PENDING independent validation: raw workbook, dates, application version, observations and consent evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
