"""Unit tests for config/source_registry.yaml and source policy decisions."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "config" / "source_registry.yaml"
DECISIONS_DIR = REPO_ROOT / "docs" / "decisions"

REQUIRED_SOURCES = [
    "openalex",
    "crossref",
    "arxiv",
    "github",
    "hackernews",
    "sec_edgar",
    "cninfo",
    "rss",
]

PHASE_1_SOURCES = ["openalex", "crossref", "arxiv", "github", "hackernews"]
PHASE_2_SOURCES = ["sec_edgar", "cninfo", "rss"]


@pytest.fixture(scope="module")
def registry_data() -> dict:
    assert REGISTRY_PATH.exists(), f"Missing {REGISTRY_PATH}"
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), "Registry YAML root must be a mapping"
    assert "sources" in data, "Registry YAML must contain 'sources' key"
    return data


def test_registry_metadata(registry_data: dict) -> None:
    assert registry_data.get("version") == "1.0.0"
    assert "updated_at" in registry_data
    assert registry_data.get("approval_authority") == "Cd (AUTH-04)"


def test_all_required_sources_present(registry_data: dict) -> None:
    sources = registry_data["sources"]
    for src in REQUIRED_SOURCES:
        assert src in sources, f"Required source '{src}' missing from registry"


def test_phase_1_sources_enabled(registry_data: dict) -> None:
    sources = registry_data["sources"]
    for src in PHASE_1_SOURCES:
        entry = sources[src]
        assert entry["enabled"] is True, f"Phase 1 source '{src}' should be enabled: true"
        assert entry.get("disable_reason") is None


def test_phase_2_sources_disabled(registry_data: dict) -> None:
    sources = registry_data["sources"]
    for src in PHASE_2_SOURCES:
        entry = sources[src]
        assert entry["enabled"] is False, f"Phase 2 source '{src}' must stay enabled: false"
        assert entry.get("disable_reason"), f"Phase 2 source '{src}' must declare disable_reason"


@pytest.mark.parametrize("src", REQUIRED_SOURCES)
def test_source_mandatory_fields(registry_data: dict, src: str) -> None:
    entry = registry_data["sources"][src]
    assert entry["source_id"] == src
    assert "source_family" in entry and entry["source_family"]
    assert "base_url" in entry
    assert "policy_snapshot_id" in entry and entry["policy_snapshot_id"].startswith("sha256-")
    assert "checked_at" in entry

    # Verify checked_at is valid ISO 8601
    dt = datetime.fromisoformat(entry["checked_at"].replace("Z", "+00:00"))
    assert dt.year >= 2026

    assert "official_doc_urls" in entry and len(entry["official_doc_urls"]) > 0
    assert "domain_whitelist" in entry
    assert "allowed_http_methods" in entry
    assert "rate_limits" in entry
    assert "required_headers" in entry
    assert "auth_needs" in entry
    assert "attribution" in entry
    assert "caching_and_retention" in entry
    assert "redistribution_rights" in entry
    assert "prohibitions" in entry and len(entry["prohibitions"]) > 0
    assert "supporting_quotes" in entry and len(entry["supporting_quotes"]) > 0


@pytest.mark.parametrize("src", PHASE_1_SOURCES)
def test_enabled_source_invariants(registry_data: dict, src: str) -> None:
    entry = registry_data["sources"][src]
    # Allowed methods must be GET only
    assert entry["allowed_http_methods"] == ["GET"], f"{src} allowed_http_methods must be ['GET']"
    # Domain whitelist must not be empty
    assert len(entry["domain_whitelist"]) > 0, f"{src} domain_whitelist must not be empty"
    for domain in entry["domain_whitelist"]:
        assert not domain.startswith("http://"), f"Domain {domain} must be FQDN without scheme"
        assert not domain.startswith("https://"), f"Domain {domain} must be FQDN without scheme"
    # Endpoints must be defined
    assert len(entry["allowed_endpoints"]) > 0, f"{src} allowed_endpoints must not be empty"
    # Supporting quotes must be non-empty and backed by quotes
    for q in entry["supporting_quotes"]:
        assert len(q.strip()) > 10, f"Quote for {src} too short: {q}"


@pytest.mark.parametrize("src", REQUIRED_SOURCES)
def test_decision_document_exists(src: str) -> None:
    doc_path = DECISIONS_DIR / f"source_policy_{src}.md"
    assert doc_path.exists(), f"Missing decision document: {doc_path}"
    content = doc_path.read_text(encoding="utf-8")
    assert len(content) > 500, f"Decision document {doc_path} is too short"
    assert "Official Documentation & Evidence URLs" in content
    assert "Allowed Endpoints & HTTP Methods" in content
    assert "Domain Whitelist" in content
    assert "Rate Limits" in content
    assert "Verbatim Supporting Quotes" in content
