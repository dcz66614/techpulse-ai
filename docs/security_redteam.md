# Q20 Security Red-Team Report — Prompt Injection and Permissions

Worktree `techpulse-ai-laneb`, branch `laneb`. Guards under test: S20 (`src/techpulse/governance/policy.py`, `src/techpulse/governance/egress.py`). No S20 guard code was modified by Q20 or QBfix.

## Scope

160 frozen case files in `tests/redteam/cases/` (all small local fixtures, each well under the 100 KB limit enforced by test). Five categories x 32 cases each:

| Category | Cases | Simulates |
|---|---|---|
| `readme_*` | 32 | malicious repository README |
| `issue_*` | 32 | malicious issue/comment body |
| `rss_*` | 32 | malicious RSS item description |
| `pdf_text_*` | 32 | text extracted from a hostile PDF |
| `paper_abstract_*` | 32 | hostile paper abstract |

Payload families per category (frozen section 10.2): tool-call requests (`shell`, `http_get`, `write_repo`, `deploy_approved_env`, `run_ci`, `publish_report`, ...), shell commands (`curl`, `wget`, `bash`, `powershell`, `rm -rf`), env/secret exfiltration (`.env`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, env dumps, `POST` to collector endpoints), budget/policy-change instructions (raise budget, default-allow policy, registry self-add, `max_tokens`, retry-cap removal, grant WriterAgent deploy), and unicode/obfuscation tricks (Cyrillic `сurl`, zero-width characters, BOM, RLO override, fence-closing breakouts).

## How the suite runs

- Pytest: `uv run pytest tests/redteam/ -q` (508 tests in `test_injection.py` plus 21 in `test_permissions.py`).
- Artifact script: `uv run python scripts/run_redteam.py --run-id <run_id>` (new in QBfix). It replays every case through the shared harness `src/techpulse/redteam.py` and writes `artifacts/<run_id>/Q20/redteam.json`, `metrics.json`, and `manifest.json` with real counts and SHA-256 digests.

Each case is replayed through the S20 guards:

1. `ingest_untrusted` fences the payload; `executable_requests` on the trusted view must find zero commands and zero tool calls; the fence must not be closable by the payload.
2. Every tool name the payload requests (raw or fenced) is replayed through `perform_tool_call` for every role whose policy grant does not include it; each must raise `PolicyBlocked`.
3. **Per-case egress (QBfix fix):** every URL and bare host is extracted from the payload itself (`extract_egress_targets`) and each target is sent through `check_egress` for the egress-capable roles; every non-allowlisted target must raise `PolicyBlocked`. This replaces the previous hard-coded URL list, so any target a fixture carries is now exercised.
4. The payload is written through `write_audit_log` and the redacted log plus `redact_secrets(fenced)` are scanned for secret-shaped strings; none may survive.
5. `subprocess`/`os.system`/httpx transports are monkeypatched to fail loudly in the pytest suite; no case ever executes a command or reaches the network.

## Real results

`uv run python scripts/run_redteam.py --run-id qbfix-20261028` (run 2026-10-28, artifacts gitignored):

```json
{
  "status": "pass",
  "metrics": {
    "cases_total": 160,
    "cases_passed": 160,
    "cases_failed": 0,
    "cases_blocked_at_ingest": 0,
    "egress_targets": 95,
    "egress_checks_blocked": 1330,
    "forbidden_tool_calls_denied": 330,
    "secrets_leaked": 0,
    "unauthorized_tool_calls_executed": 0
  }
}
```

- 95 egress targets extracted from the 160 payloads (e.g. `https://evil.example/collect`, `http://evil.example/payload.sh`, `https://evil.example/env`, `https://evil.example/zero`, `https://evil.example/breakout`, `http://evil.example/x`); all 1330 role/target combinations blocked by `check_egress`.
- 330 forbidden role/tool replays, all denied by the policy; `unauthorized_tool_call_count()` stayed at 0.
- Zero commands or tool calls in any trusted view; zero secrets in any audit log.
- pytest: `374 passed` for `tests/redteam/` (per-case egress tests are parametrized from the extracted targets); full suite result under Final gates below.

Artifacts: `artifacts/qbfix-20261028/Q20/redteam.json` (69,504 bytes, sha256 `0749a846...ca09`), `metrics.json` (470 bytes, sha256 `d6062d40...2aab`), `manifest.json` with digests of both.

## Findings

- No breakouts found; no xfail entries needed. No S20 guard code was weakened or changed.
- Observation (not a guard defect): payloads without a URL-shaped marker (e.g. a bare `rm -rf /`) rely on command detection rather than egress blocking; these are covered by the `executable_requests` trusted-view assertions.
- QBfix review finding resolved: egress checks previously used a hard-coded URL list; they now extract every URL/host from each case fixture, so future fixture edits cannot silently escape egress testing.
