# Threat model: the web view and the event log

**Status:** living document, current as of v0.6.3. Decisions are recorded in
[ADR-0018](adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md) (the
read-only view) and [ADR-0021](adr/0021-service-auth-is-a-bearer-token.md)
(auth, transport, rate limiting).

**Scope.** `harness serve` (FastAPI + SSE), the SQLite event log it reads, and
the bearer token that guards it. Out of scope: the model gateway, tool
implementations, the host OS, and any other exporter (Langfuse, MLflow). Those
have their own data flows and are named in the non-claims below.

This document is written to be read by a reviewer who has to sign something. It
states what the code does, what it deliberately does not do, and what the
operator must supply.

---

## 1. What is protected

| Asset | Where it lives | Classification |
|---|---|---|
| Run transcripts: prompts, model output, reasoning, tool arguments and results | `run_events.payload`, redacted on write | **Confidential**; may contain personal data |
| Session titles, model names, timings, token counts | `sessions`, `runs` | Internal |
| The bearer token | `RIGGING_SERVICE_TOKEN` (env), browser `sessionStorage` | **Secret** |
| The database file | one SQLite file on disk (`rigging.db` by default) | **Confidential** |

The transcript is the sensitive asset. "Read-only" is not "harmless": a run can
contain a customer's order, a support ticket, or a model's chain of thought.
Redaction (hard rule 5, `rigging/redact.py`) is applied **before** insert, and
`tests/test_redact.py` asserts on the database file itself. Redaction is
pattern-based; it reduces exposure, it is not a guarantee (see §6).

### Data flow

```
operator browser ──TLS──▶ reverse proxy ──HTTP/loopback──▶ uvicorn (FastAPI)
                                                              │  read-only SQLite
                                                              ▼
                                                          rigging.db
```

There is exactly one write path into the database — the agent loop — and the
service opens it with SQLite `mode=ro`. The browser never sends run data back.

---

## 2. Actors and trust boundaries

| Actor | Capability | Trust |
|---|---|---|
| Operator | holds the bearer token, reads transcripts | trusted, but a single shared identity |
| Network attacker | can observe or inject traffic if TLS is absent | untrusted |
| Same-origin script (XSS) | runs with the page's origin, can read `sessionStorage` | untrusted; assumed possible |
| Local user on the host | can read the database file if file permissions allow | outside the app's control |
| Reverse proxy | terminates TLS, forwards to loopback | trusted component, operator-managed |

The boundary that matters is the proxy-to-uvicorn hop. It is plain HTTP on
loopback by default, which is safe only because it does not leave the host.

---

## 3. Threats and controls

| ID | Threat | Control | Residual risk |
|---|---|---|---|
| T1 | Bearer token sniffed on plain HTTP | `harness serve` **refuses a non-loopback bind with a token over plain HTTP** unless `--ssl-certfile/--ssl-keyfile` are given or `RIGGING_SERVICE_ALLOW_PLAINTEXT=1` is set; the documented deployment is TLS at a proxy with a loopback bind (§5) | An operator who sets the opt-in flag on an untrusted network is on their own; the refusal is a guard rail, not a network control |
| T2 | Token stolen by XSS and reused | No third-party scripts, no CDN, no build step; CSP with a per-response nonce blocks external script/style and framing; every payload is escaped (`esc`) or set with `textContent`; token is rotatable | Any same-origin script can read `sessionStorage`. In read-only mode that buys transcripts; in chat mode it could also start a run or approve a tool, so the CSP and the no-third-party-script rule are load-bearing (ADR-0023) |
| T3 | Token guessing / credential stuffing | Token compared with `hmac.compare_digest`; per-client rate limit checked **before** auth, `429` + `Retry-After` | No account lockout and no per-user identity; use a high-entropy random token (≥ 32 bytes) |
| T4 | Request flood, or a client pinning workers with SSE | Per-client fixed-window limiter (240/min default, `RIGGING_SERVICE_RATE_LIMIT`, `0` disables); the key is the peer address, or the `X-Forwarded-For` client only when the peer is in `RIGGING_SERVICE_TRUSTED_PROXIES`; SSE ends on `idle` and `timeout` | In-process, resets on restart. If the proxy is untrusted or does not set the header, all clients share one bucket, so a single script can exhaust it and deny others (availability, not auth). The proxy's own limiter is the primary control |
| T5 | Transcript disclosure through a redaction gap | Redaction before insert; tests assert on the DB file; the service never re-reads a raw payload because none is stored | Pattern-based redaction misses unstructured identifiers. Treat the DB as confidential regardless |
| T6 | Service exposed without auth by mistake | Loopback default; startup refuses non-loopback without a token; `GET /` serves no run data | A misconfigured proxy or a permissive firewall is outside the app |
| T7 | Local user or stolen backup reads the DB | The app does not add encryption at rest; rely on file permissions (`0600`), full-disk encryption, and encrypted backups | Same-host user with file access reads everything. No app-level encryption |
| T8 | CSRF / cross-origin reads, or a page the operator visits POSTing to a loopback port | The credential is a header, not a cookie, so a cross-site form or image cannot authenticate; no CORS middleware, so a cross-origin JSON POST fails its preflight; chat additionally **requires a token even on loopback** (ADR-0023) | A browser extension with host access is outside the model |
| T9 | Clickjacking | `frame-ancestors 'none'` + `X-Frame-Options: DENY` | — |
| T10 | Run payload injects script into the page | Payloads escaped or assigned with `textContent`; CSP nonce; no `eval`, no `innerHTML` with unescaped data | A future edit that adds an unescaped sink would be a regression; the browser test loads real data |
| T11 | Supply chain | Four core dependencies (`httpx`, `pydantic`, `pydantic-settings`, `pyyaml`); the service is an extra; no CDN; `uv.lock` committed | Upstream compromise is not modelled; pin and audit in the deployment pipeline |
| T12 | Token has no expiry or revocation | Rotation is a restart with a comma-separated list (`old,new`) | No per-token expiry, no revocation list, no session concept |
| T14 | The chat write path spends money or runs tools without the operator | Opt-in (`--manifest`); token required even on loopback; one turn at a time per session (`409`); the loop's `ApprovalPolicy` still gates every side-effecting tool, and `WebApproval` only carries the page's answer | A holder of the token can start runs by design; there is no per-user quota. The demo DB's `refund_order` records intent, it does not move money |
| T13 | No record of **who** read what | The service logs nothing by default; the proxy can log requests | No per-user audit trail. A regulated deployment needs OIDC + audit (see §7) |

---

## 4. Controls and where they are tested

| Control | Code | Test |
|---|---|---|
| Token compare, no timing leak | `rigging/service/app.py::_auth_dependency` | `test_service.py::test_a_wrong_token_is_rejected` |
| Loopback / TLS bind rule | `check_bind` | `test_a_token_over_plain_http_is_refused_off_loopback` |
| Rate limit before auth | `_rate_limit_dependency`, `service/ratelimit.py` | `test_ratelimit.py`, `test_the_limit_is_checked_before_the_token` |
| Security headers + CSP nonce | `security_headers` middleware | `test_security_headers_and_a_matching_csp_nonce` |
| Page actually runs under CSP | `INDEX_HTML` nonce attributes | `test_service_browser.py` (headless Chromium) |
| Read-only database | `_connect` (`mode=ro`) | `test_the_service_never_writes` |
| Redaction before insert | `rigging/redact.py` | `test_redact.py` (asserts on the DB file) |
| No write/launch/approve route without a manifest | route list is six GETs | `test_the_service_never_writes` |
| Chat refuses to exist without a token | `create_app` guard | `test_chat_refuses_to_exist_without_a_token` |
| Approval is answered by the loop's policy, not the page | `service/chat.py::WebApproval` | `test_approval_is_answered_from_the_page`, `test_denied_approval_blocks_the_tool` |
| The page's write path works in a real browser | `INDEX_HTML` | `test_service_browser.py::test_the_page_can_run_a_turn_and_approve_it` |

`make check` runs all of it offline. `make test-browser` runs the page in
headless Chromium, because a `200` is not evidence that the page renders.

---

## 5. Deployment baseline

The supported exposed deployment is: **TLS at a reverse proxy, uvicorn on
loopback, token from the environment.**

```bash
# uvicorn stays on loopback; the proxy owns the certificate
RIGGING_SERVICE_TOKEN="$(openssl rand -base64 32)" \
  uv run harness serve --host 127.0.0.1 --port 8000
```

Caddy (automatic certificates):

```caddyfile
view.example.com {
    reverse_proxy 127.0.0.1:8000   # Caddy appends X-Forwarded-For by default
}
```

nginx (TLS already provisioned):

```nginx
limit_req_zone $binary_remote_addr zone=rigging:10m rate=10r/s;

server {
    listen 443 ssl;
    http2 on;
    server_name view.example.com;

    ssl_certificate     /etc/ssl/view.example.com.pem;
    ssl_certificate_key /etc/ssl/view.example.com.key;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        limit_req zone=rigging burst=20 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  # append
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;          # SSE must not be buffered
        proxy_read_timeout 1h;
    }
}
```

### How the rate-limit key is derived

The app never parses `X-Forwarded-For`. It reads `request.client.host`, and the
ASGI server decides what that is **before** the app runs. `harness serve` passes
`RIGGING_SERVICE_TRUSTED_PROXIES` (default `127.0.0.1,::1`) to uvicorn's
`forwarded_allow_ips`; uvicorn rewrites the client only when the immediate peer
is in that list, and then takes the **rightmost address that is not itself a
trusted proxy**. A header sent by an untrusted peer is ignored.

| Immediate peer | `X-Forwarded-For` | Key | Consequence |
|---|---|---|---|
| not trusted | anything | the peer address | spoofing is impossible; that peer's requests share one bucket |
| trusted | absent | the peer address | misconfigured proxy: every user shares one bucket |
| trusted | `client` or `client, proxy` | the client | one bucket per client |

Rules that follow:

- The proxy must **set or append** the header (`$proxy_add_x_forwarded_for` in
  nginx; Caddy does it by default). Never forward a client-supplied header
  unchanged to a hop the app trusts.
- If the proxy is on another host, set `RIGGING_SERVICE_TRUSTED_PROXIES` to its
  address. `*` and any `/0` network are refused at startup: they would let any
  client pick its own bucket and frame another client.
- With no trusted proxy the limit is one shared bucket. That is safe against
  spoofing, but a single script can exhaust it and deny service to everyone.
  Treat the app limiter as a backstop and rate-limit at the proxy, where the real
  client address is known.

Direct TLS is possible without a proxy when a certificate is already on the
host:

```bash
uv run harness serve --host 0.0.0.0 --port 8443 \
  --ssl-certfile /etc/ssl/view.pem --ssl-keyfile /etc/ssl/view.key
```

Operator checklist:

- [ ] `RIGGING_SERVICE_TOKEN` is a random value from a secret manager, not a
      shell literal; at least 32 bytes.
- [ ] `harness serve` binds `127.0.0.1`; only the proxy listens publicly.
- [ ] TLS 1.2+ at the proxy; HSTS on the public name.
- [ ] The proxy sets `X-Forwarded-For` and its own request limit, and its
      address is in `RIGGING_SERVICE_TRUSTED_PROXIES` (loopback by default).
- [ ] `rigging.db` is `0600`, on an encrypted volume; backups are encrypted.
- [ ] A retention job deletes old runs (the app does not expire them).
- [ ] The proxy logs requests (status, path, client IP) for incident review.

---

## 6. Explicit non-claims

A reviewer should not assume any of the following, because the code does not
provide it:

- **No encryption at rest.** SQLite is plaintext; the app relies on the OS and
  the volume. No KMS, no field-level encryption.
- **Redaction is not DLP.** `redact.py` is a set of patterns. It is tested, but
  it cannot certify that a transcript is free of personal data.
- **No per-user authentication or authorization.** One shared token, one
  read-only scope. No RBAC, no tenants, no per-run access control.
- **No audit trail of reads.** The service does not record who viewed which run.
  The proxy's access log is the only record.
- **No token expiry or revocation.** Rotation is a restart with both values.
- **No availability guarantee.** One process, one SQLite file, no replication.
- **The rate limiter is a backstop.** It is in-process, fixed-window, and resets
  on restart. Its key is only per-client when the proxy is trusted and sets
  `X-Forwarded-For`; otherwise one script can exhaust a shared bucket and deny
  service to others. It is not a WAF.
- **No mTLS, WAF, SIEM forwarding, or data-residency control.** Those are
  deployment concerns.
- **The harness's outbound calls are out of scope.** Model gateways, Langfuse
  and MLflow receive data according to their own configuration; this document
  covers only what the web view stores and serves.

---

## 7. What a regulated deployment would add

In rough order of value for a bank or insurer review:

1. **OIDC / SSO** with a read-only scope, so access is per-person and revocable
   (the deferred half of ADR-0021).
2. **Per-user audit** of views and exports, forwarded to the SIEM.
3. **Encryption at rest** with a managed key, and a documented key-rotation
   procedure.
4. **Retention and erasure**: a scheduled delete, and a way to answer a subject
   access request over the event log.
5. **Network controls**: mTLS between proxy and app, a WAF in front, egress
   restrictions on the host.
6. **High availability**: replicated storage and more than one process; today's
   SQLite file is a single point of failure.

Until then, the honest summary is: a single-operator, read-only development
window that can be exposed safely behind a TLS proxy with one rotatable token,
and that must be treated as holding confidential data.

---

## 8. Verification commands

```bash
make check                       # 188 offline tests: auth, headers, limiter, redaction
make test-browser                # the page renders under the CSP in Chromium
uv run pytest tests/test_service_proxy.py   # XFF trusted vs untrusted peer

RIGGING_SERVICE_TOKEN=demo uv run harness serve &
curl -si http://127.0.0.1:8000/api/runs | head -1          # 401
curl -si -H 'Authorization: Bearer demo' \
     http://127.0.0.1:8000/api/runs | grep -i 'content-security-policy'
for i in $(seq 1 300); do curl -s -o /dev/null -w '%{http_code}\n' \
     -H 'Authorization: Bearer demo' http://127.0.0.1:8000/api/runs; done | sort | uniq -c
                                                           # 200s, then 429s
RIGGING_SERVICE_TOKEN=demo uv run harness serve --host 0.0.0.0   # exits 2
```
