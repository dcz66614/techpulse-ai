from pathlib import Path

from detect_secrets.core.scan import scan_file
from detect_secrets.settings import default_settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_secret_sample.txt"


def test_secret_scanner_flags_fake_github_token() -> None:
    with default_settings():
        findings = list(scan_file(str(FIXTURE)))

    assert findings, "scanner did not flag the fake token fixture"
    assert any(finding.type == "GitHub Token" for finding in findings)
