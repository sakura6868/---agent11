"""Check a clean local release without printing credentials or calling a model."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8015")
    args = parser.parse_args()
    checks = []

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})
        print(name, "PASS" if passed else "FAIL")

    with ZipFile(args.archive) as bundle:
        names = bundle.namelist()
        relative = [n.split("/", 1)[1] for n in names]
        check("final_video_only", [n for n in relative if n.startswith("demo/")] == ["demo/演示视频.mp4"])
        check("pytest_configuration", "pytest.ini" in relative)
        check("no_private_environment", not any(Path(n).name.startswith(".env") and Path(n).name != ".env.example" for n in relative))
        check("no_database", not any(any(marker in n.lower() for marker in (".db", ".sqlite")) for n in relative))
        # Compare private local values without exposing them in output or reports.
        secrets = []
        for line in ((ROOT / ".env").read_text(encoding="utf-8").splitlines() if (ROOT / ".env").exists() else []):
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET")) and len(value.strip()) >= 12:
                    secrets.append(value.strip().strip('"\'').encode())
        check("no_local_secrets", not any(secret in bundle.read(n) for n in names for secret in secrets))

    base = args.base_url.rstrip("/")
    def get(path, **kwargs):
        response = requests.get(base + path, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    health = get("/health")
    check("health_connected", health.get("database") == "connected")
    check("health_no_connection_information", not any(marker in json.dumps(health).lower() for marker in ("database_url", "postgres://", "postgresql://", "sqlite:", "password", "hostname")))
    check("frontend_available", requests.get(base, timeout=10).status_code == 200)
    check("model_disabled", not get("/api/agent/llm-status").get("enabled"))
    all_items = get("/api/competitions")
    ready = get("/api/competitions?readiness=ready")
    candidates = get("/api/competitions?readiness=candidate")
    expected_count = len(list((ROOT / "data/ground_truth/samples").glob("*.json")))
    check("ground_truth_records_initialized", len(all_items) == expected_count)
    check("catalog_partition", len(ready) + len(candidates) == len(all_items) and all(c["recommendation_ready"] for c in ready) and not any(c["recommendation_ready"] for c in candidates))
    response = requests.post(base + "/api/auth/login", json={"username": "test", "password": "test123"}, timeout=10)
    check("demo_login", response.status_code == 200)
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    recommendations = get("/api/users/test/recommendations", headers=headers)
    check("only_formal_recommendations", bool(recommendations) and all(r.get("score") is not None and r.get("eligible") for r in recommendations))
    response = requests.post(base + "/api/users/test/projects", headers=headers, json={"competition_id": recommendations[0]["competition_id"]}, timeout=10)
    check("join_formal_project", response.status_code == 200)
    project = response.json()
    kinds = {item["item_type"] for item in project["items"]}
    check("tasks_and_materials", {"task", "material"}.issubset(kinds))
    project_id = project["project_id"]
    calendar = requests.get(base + f"/api/users/test/projects/{project_id}/calendar.ics", headers=headers, timeout=10)
    check("calendar_export", calendar.status_code == 200 and "BEGIN:VCALENDAR" in calendar.text and "BEGIN:VEVENT" in calendar.text)
    deleted = requests.delete(base + f"/api/users/test/projects/{project_id}", headers=headers, timeout=10)
    check("project_delete", deleted.status_code == 200)
    candidate = candidates[0]["competition_id"]
    response = requests.post(base + "/api/users/test/projects", headers=headers, json={"competition_id": candidate}, timeout=10)
    check("candidate_project_blocked", response.status_code == 409)
    for endpoint in ("upload", "parse", "competitions", "competitions/bulk"):
        response = requests.post(base + "/api/admin/" + endpoint, json={}, timeout=10)
        check("admin_closed_" + endpoint, response.status_code == 503)
    answer = get("/api/agent/ask", params={"question": "报名截止是什么时候？", "competition_id": "mathorcup_data_2026"})
    check("canonical_deadline", "2026-10-23 12:00" in answer["answer"] and bool(answer["citations"]))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "clean extracted release, isolated database, existing Python environment, offline HTTP", "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(), "records": len(all_items), "registerable": len(ready), "checks": checks, "passed": sum(c["passed"] for c in checks), "total": len(checks)}
    (ROOT / "evals/release_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
