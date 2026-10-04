"""Check curated evidence against the actual downloaded official PDF."""

import hashlib
import json
import re
from pathlib import Path

import pdfplumber


ROOT = Path(__file__).resolve().parents[1]


def test_core_official_documents_are_available_in_a_clean_checkout():
    manifest = json.loads((ROOT / "data/core_competitions_manifest.json").read_text(encoding="utf-8"))
    for source in manifest["competitions"]:
        path = (ROOT / source["document_path"]).resolve()
        assert path.is_relative_to(ROOT / "data/official_sources")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
        assert (ROOT / source["ground_truth_path"]).is_file()


def test_official_pdf_fingerprint_pages_and_quoted_evidence():
    manifest = json.loads((ROOT / "data/official_sources/manifest.json").read_text(encoding="utf-8"))
    for source in manifest["sources"]:
        path = ROOT / source["document_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
        record = json.loads((ROOT / "data/ground_truth/samples" / (source["competition_id"] + ".json")).read_text(encoding="utf-8"))
        with pdfplumber.open(path) as document:
            assert len(document.pages) == source["pages"]
            for evidence in record["evidence"]:
                page = document.pages[evidence["page"] - 1]
                normalized = re.sub(r"\s+", "", page.extract_text() or "")
                quote = re.sub(r"\s+", "", evidence["source_text"])
                assert quote in normalized, evidence["field"]
                assert evidence["document_sha256"] == source["sha256"]
