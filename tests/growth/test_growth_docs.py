import re
from pathlib import Path

import pytest

GROWTH_DIR = Path(__file__).resolve().parents[2] / "growth"
REQUIRED_FILES = ("README.md", "interview_guide.md", "sample_template.md")

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-()]{7,}\d)(?!\d)")
HTTP_PATTERN = re.compile(r"https?://", re.IGNORECASE)
PLACEHOLDER_URL_PATTERN = re.compile(r"https?://<[^>\s]+>", re.IGNORECASE)


def _read_growth_file(name: str) -> str:
    path = GROWTH_DIR / name
    assert path.is_file(), f"missing growth file: {name}"
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{name} is not valid UTF-8: {exc}")


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_growth_file_exists_and_is_utf8(name: str) -> None:
    assert _read_growth_file(name).strip(), f"{name} is empty"


def test_interview_guide_has_auth06_and_no_contact_patterns() -> None:
    text = _read_growth_file("interview_guide.md")
    assert "AUTH-06" in text
    assert not EMAIL_PATTERN.search(text), "interview guide must not contain email addresses"
    assert not PHONE_PATTERN.search(text), "interview guide must not contain phone numbers"


def test_sample_template_has_required_fields() -> None:
    text = _read_growth_file("sample_template.md")
    for token in ("Q10", "as_of", "AI 生成"):
        assert token in text, f"sample template missing {token!r}"
    assert "q10_status: PENDING" in text


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_growth_files_have_only_placeholder_urls(name: str) -> None:
    text = _read_growth_file(name)
    for match in HTTP_PATTERN.finditer(text):
        assert PLACEHOLDER_URL_PATTERN.match(text, match.start()), (
            f"non-placeholder http(s) URL in {name}: {text[match.start():match.start() + 40]!r}"
        )
