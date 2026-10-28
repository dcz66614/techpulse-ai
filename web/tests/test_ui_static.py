"""Static checks for the dashboard source. No browser and no network."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"
JS = WEB / "js" / "dashboard.js"
CSS = WEB / "css" / "app.css"
HTML = WEB / "index.html"
SPEC = ROOT / "docs" / "ui_spec.md"

VIEWS = {
    "view-leaderboard",
    "view-trend",
    "view-evidence",
    "view-contrary",
    "view-reports",
    "view-status",
}
STATES = {"loading", "empty", "error", "degraded", "ready"}
SECRET_PATTERNS = (
    r"BEGIN [A-Z ]*PRIVATE KEY",
    r"AKIA[0-9A-Z]{16}",
    r"sk-[A-Za-z0-9]{10,}",
    r"TECHPULSE_[A-Z0-9_]+\s*=",
    r"(?i)(api[_-]?key|password|secret|cursor_key|hmac)\s*[:=]\s*['\"][^'\"]+['\"]",
    r"(?i)bearer\s+[A-Za-z0-9._-]{8,}",
    r"localStorage",
    r"sessionStorage",
    r"document\.cookie",
    r"indexedDB",
)
BANNED_LITERALS = (
    "reader-token",
    "test-cursor-key",
    "report-w1",
    "ev-ok",
    "MOCK",
    "sampleTopics",
    "placeholderData",
    "fakeData",
    "dummyData",
)


class _Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.view_states: dict[str, set[str]] = {}
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        section_id = found.get("id")
        if tag == "section" and section_id:
            self.stack.append(section_id)
            self.view_states.setdefault(section_id, set())
        state = found.get("data-state")
        if state and self.stack:
            self.view_states[self.stack[-1]].add(state)

    def handle_endtag(self, tag: str) -> None:
        if tag == "section" and self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _client_files() -> list[Path]:
    return sorted(path for path in WEB.rglob("*") if path.is_file())


def test_views_declare_every_state_and_the_ai_label() -> None:
    parser = _Markup()
    page = HTML.read_text(encoding="utf-8")
    parser.feed(page)
    assert set(parser.view_states) == VIEWS
    for view, states in parser.view_states.items():
        assert states == STATES, view
    text = "".join(parser.text)
    assert "AI 生成" in text
    assert 'id="as-of"' in page
    assert "data-as-of" in page
    assert 'src="js/dashboard.js"' in page
    assert 'href="css/app.css"' in page
    assert "暂无已发布主题" in text
    assert "暂无来源证据" in text
    assert "暂无反证" in text
    assert "暂无已发布报表" in text
    assert "暂无系统状态" in text
    assert "页面没有预置数据" in text


def test_state_machine_uses_the_payload_and_hides_unpublished_rows() -> None:
    script = JS.read_text(encoding="utf-8")
    assert "function isPublic(row)" in script
    assert "row.qa_pass !== true" in script
    assert "row.signed !== true" in script
    assert "function panels(httpStatus, payload)" in script
    assert 'names.push("loading")' not in script
    assert 'if (httpStatus === "loading" || httpStatus === "error") return [httpStatus];' in script
    assert 'names.push("degraded")' in script
    assert 'names.push("error")' not in script
    assert 'return ["error"];' in script
    assert 'if (visibleCount(payload) === 0) names.push("empty");' in script
    assert 'names.push("ready")' in script
    assert "function statusPanels(" in script
    assert 'return degraded ? ["degraded", "empty"] : ["empty"];' in script
    calls = re.findall(r"(?<!function )sourceLink\(([^)]*)\)", script)
    assert calls
    assert all("source_url" in call for call in calls)
    assert 'anchor.target = "_blank"' in script
    assert 'anchor.rel = "noopener noreferrer"' in script
    assert 'method: "GET"' in script
    assert 'credentials: "omit"' in script
    calls = [line.strip() for line in script.splitlines() if "credentialValue()" in line]
    assert calls == [
        "function credentialValue() {",
        "var value = credentialValue();",
    ]


def test_client_has_no_secrets_or_hard_coded_data() -> None:
    for path in _client_files():
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            assert re.search(pattern, text) is None, path.name
        for banned in BANNED_LITERALS:
            assert banned not in text, path.name
        assert re.search(r"https?://[A-Za-z0-9]", text) is None, path.name
        assert re.search(r"items\s*:\s*\[\s*\{", text) is None, path.name
    script = JS.read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script
    assert re.search(r"\beval\s*\(", script) is None
    assert "Date.now" not in script
    assert "new Date" not in script
    assert "/metrics" not in script
    assert "/docs" not in script
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert method not in script
    assert re.search(r"\|\|\s*\[\s*[^\]\s]", script) is None
    assert re.search(r"\b\d+\.\d+\b", script) is None


def test_css_has_no_horizontal_overflow_rules() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert "box-sizing: border-box" in css
    assert "overflow-x: hidden" in css
    assert "overflow-wrap: anywhere" in css
    assert "@media (max-width: 375px)" in css
    assert "@media (min-width: 1280px)" in css
    assert "nowrap" not in css
    assert "100vw" not in css
    assert "max-width: 72rem" in css
    layout = re.sub(r"@media\s*\([^)]*\)", "", css)
    for match in re.finditer(r"(?<![\w-])width\s*:\s*(\d+)px", layout):
        assert int(match.group(1)) <= 375
    for match in re.finditer(r"min-width\s*:\s*(\d+(?:\.\d+)?)(px|rem|em)", layout):
        number = float(match.group(1))
        unit = match.group(2)
        pixels = number * 16 if unit in {"rem", "em"} else number
        assert pixels <= 320
    for match in re.finditer(r"max-width\s*:\s*(\d+(?:\.\d+)?)(px|rem|em)", layout):
        number = float(match.group(1))
        unit = match.group(2)
        pixels = number * 16 if unit == "rem" else number
        assert pixels <= 1280


def test_ui_spec_covers_the_views_and_states() -> None:
    text = SPEC.read_text(encoding="utf-8")
    for phrase in (
        "loading",
        "empty",
        "error",
        "degraded",
        "AI 生成",
        "as_of",
        "qa_pass",
        "/ui",
        "/v1/trends",
        "/v1/evidence",
        "/v1/reports",
        "/health/live",
        "/health/ready",
        "source_url",
    ):
        assert phrase in text
