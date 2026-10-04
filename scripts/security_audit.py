"""Scan tracked source or a submission ZIP. Emit locations and hashes, never secret values."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "database_credentials": re.compile(rb"postgres(?:ql)?://[^\s:/\"']+:[^\s@\"']+@[^\s/\"']+", re.I),
    "provider_key": re.compile(rb"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "assigned_secret": re.compile(rb"(?:AGENT_LLM_API_KEY|ADMIN_API_TOKEN|AUTH_TOKEN_SECRET|WEB_SEARCH_API_KEY)[ \t]*=[ \t]*[\"']?([^\s\"'#]{16,})"),
}
PUBLIC_PLACEHOLDERS = (b"example", b"replace-with", b"changeme", b"your-", b"os.getenv", b"isolated-", b"test-", b"planner-demo-", b"monkeypatch", b"getenv")


def findings(name: str, content: bytes, commit=None):
    found = []
    if b"\x00" in content[:4096] or name.endswith((".mp4", ".pdf", ".png", ".jpg", ".webm", ".woff2")):
        return found
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            value = match.group(1) if kind == "assigned_secret" else match.group()
            if any(word in value.lower() for word in PUBLIC_PLACEHOLDERS):
                continue
            # Source assignment to environment lookups and documented shell variables is not a credential.
            if kind == "assigned_secret" and value.startswith((b"$", b"(", b"{", b"secrets.", b"\"")):
                continue
            found.append({"path": name, "line": content[:match.start()].count(b"\n") + 1,
                          "kind": kind, "fingerprint": hashlib.sha256(value).hexdigest()[:16],
                          **({"commit": commit} if commit else {})})
    return found


def audit_archive(path: Path):
    found = []
    with ZipFile(path) as archive:
        for member in archive.namelist():
            if not member.endswith("/"):
                found.extend(findings(member, archive.read(member)))
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "evals/security_audit.json")
    args = parser.parse_args()
    if args.archive:
        current = audit_archive(args.archive)
    else:
        paths = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode("utf-8").split("\0")
        current = [item for name in paths if name and (ROOT / name).is_file()
                   for item in findings(name, (ROOT / name).read_bytes())]
    historical = []
    if args.history:
        # Scan deleted as well as added lines in patches; a deletion does not revoke a credential.
        output = subprocess.check_output(["git", "log", "--all", "--format=COMMIT:%H", "-p", "--no-ext-diff"], cwd=ROOT)
        commit, path = None, "unknown"
        for line in output.splitlines():
            if line.startswith(b"COMMIT:"):
                commit = line[7:].decode("ascii")
            elif line.startswith(b"diff --git "):
                path = line.decode("utf-8", errors="replace").split(" b/", 1)[-1]
            elif line.startswith((b"+", b"-")) and not line.startswith((b"+++", b"---")):
                historical.extend(findings(path, line[1:], commit=commit))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "current_findings": current,
              "historical_findings": historical, "current_clean": not current,
              "scope": "archive" if args.archive else "tracked working-tree files",
              "limitations": "Pattern-based scan; not a full security audit. Historical exposure requires provider-side rotation.",
              "cloud_rotation_verified": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"current_findings": len(current), "historical_findings": len(historical), "report": str(args.output)}, ensure_ascii=False))
    return 1 if current else 0


if __name__ == "__main__":
    raise SystemExit(main())
