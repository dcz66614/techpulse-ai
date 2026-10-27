# S20 isolation

S20 enforces egress and tool permissions in the application process. This
machine has no container runtime, so Collector, Cleaner, Writer, and QA are
not placed in separate network namespaces.

## Enforced now

`config/policy.yaml` is default deny. Each role from section 1.2 has
`network: none` or `network: allowlisted_get`, and a list of tools.

`check_egress(role, method, url)` allows only `GET` and `HEAD`, only for a
role whose network is `allowlisted_get` (CollectorAgent), and only to hosts
from enabled sources in `config/source_registry.yaml` when that file exists.
Otherwise it uses `network_allowlist.domains` in `config/policy.yaml`.
CleanerAgent, WriterAgent, and QAAgent have no network. Any other outbound
call raises `PolicyBlocked`. `GuardedTransport`, `AsyncGuardedTransport`, and
`egress_request_hook` run that check for httpx.

`authorize_tool_call` allows a tool only when the role lists it, then
validates arguments with Pydantic. `put_raw`, `write_evidence`, and
`update_task_status` use the P10 `RawRecord`, `EvidenceItem`, and
`TaskResult` models. Third-party text goes through `UntrustedData`, which
escapes fence breakouts and is not executed. `write_audit_log` redacts
secret-looking strings before they are written.

HTTP `PUT` to a third party is denied. Collector `put_raw` is an internal
tool that stores a `RawRecord`, not an outbound write.

## Future work: containers and secret storage

When a container runtime is available, run the roles in separate containers.
Give Cleaner, Writer, and QA no outbound route. Give the collector container
an egress allow-list equal to the enabled source domains and no other route.
Keep production secrets in a secret manager. This ticket does not start
containers and does not read production secrets.
