"""G10 samples are regenerated from the fixture pipeline and stay traceable."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.build_samples import build_samples

from techpulse.governance.pii import contains_pii

ROOT = Path(__file__).resolve().parents[2]
GROWTH = ROOT / "growth"
SAMPLE_NAMES = ("sample_1.md", "sample_2.md")
FIXTURE_ROOTS = (ROOT / "tests" / "fixtures", ROOT / "data")
URL_PATTERN = re.compile(r"https?://[^\s|）】>\"'`)\]]+")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SHA256_PATTERN = re.compile(r"\b[0-9a-f]{64}\b")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".xml", ".yaml", ".yml"}


def test_samples_regenerate_identically() -> None:
    first = build_samples(ROOT)
    second = build_samples(ROOT)
    assert first.manifest == second.manifest
    assert first.metrics == second.metrics
    by_name = {sample.name: sample for sample in first.samples}
    again = {sample.name: sample for sample in second.samples}
    assert set(by_name) == {"sample_1", "sample_2"}
    for name in SAMPLE_NAMES:
        sample_id = name.removesuffix(".md")
        committed = _read(GROWTH / name)
        assert by_name[sample_id].markdown == committed
        assert again[sample_id].markdown == committed


def test_each_sample_carries_q10_status_and_publication_label() -> None:
    for name in SAMPLE_NAMES:
        text = _read(GROWTH / name)
        q10_status = _field(text, "q10_status")
        qa_pass = _field(text, "qa_pass")
        publication = _field(text, "publication_status")
        assert q10_status in {"pass", "fail"}
        assert "AI 生成" in text
        if qa_pass == "true":
            assert q10_status == "pass"
            assert publication == "publishable"
        else:
            assert qa_pass == "false"
            assert q10_status == "fail"
            assert publication == "internal demo only"
            assert "publishable" not in text


def test_sample_urls_come_from_fixtures() -> None:
    allowed = _fixture_urls()
    assert allowed, "fixture corpus has no URLs"
    for name in SAMPLE_NAMES:
        text = _read(GROWTH / name)
        found = _urls(text)
        assert found, f"{name} has no source URL"
        invented = found - allowed
        assert not invented, f"invented URLs in {name}: {sorted(invented)}"


def test_samples_have_no_personal_contact_data() -> None:
    for name in SAMPLE_NAMES:
        text = _read(GROWTH / name)
        assert EMAIL_PATTERN.search(text) is None
        # Content hashes are 64 hex characters. An 18-digit run inside one
        # matches the national-id scanner, so those digests are removed first.
        assert not contains_pii(SHA256_PATTERN.sub(" ", text))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _field(text: str, name: str) -> str:
    matches = re.findall(rf"^- {name}: (.+)$", text, flags=re.MULTILINE)
    assert matches, f"missing {name}"
    assert len(set(matches)) == 1, f"conflicting {name}: {matches}"
    found = matches[0]
    assert isinstance(found, str)
    return found


def _fixture_urls() -> set[str]:
    found: set[str] = set()
    for folder in FIXTURE_ROOTS:
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            found.update(_urls(path.read_text(encoding="utf-8")))
    return found


def _urls(text: str) -> set[str]:
    return {match.group(0).rstrip(".,") for match in URL_PATTERN.finditer(text)}
