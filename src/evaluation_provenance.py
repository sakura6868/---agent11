"""Stable fingerprints for frozen evaluations, excluding database surrogate IDs."""

import hashlib
import json
from pathlib import Path


def dataset_sha256(root: Path) -> str:
    files = sorted((root / "data/ground_truth/samples").glob("*.json"))
    return hashlib.sha256(b"".join(path.read_bytes().replace(b"\r\n", b"\n") for path in files)).hexdigest()


def code_sha256(root: Path) -> str:
    files = sorted((root / "src").rglob("*.py"))
    files += sorted((root / "evals").glob("*.py"))
    files += sorted((root / "tests").rglob("*.py"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def citation_identity(citation: dict) -> str:
    payload = dict(citation)
    for key in ("citation_id", "document_id"):
        payload.pop(key, None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def competition_snapshot_sha256(competitions) -> str:
    rows = []
    for comp in sorted(competitions, key=lambda item: item.competition_id):
        payload = comp.model_dump(mode="json")
        payload["evidence"] = sorted(citation_identity(item) for item in payload["evidence"])
        rows.append(payload)
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evaluation_is_current(report: dict, root: Path, competitions) -> bool:
    return bool(report.get("evaluated_at")) and all((
        report.get("dataset_sha256") == dataset_sha256(root),
        report.get("code_sha256") == code_sha256(root),
        report.get("competition_snapshot_sha256") == competition_snapshot_sha256(competitions),
    ))
