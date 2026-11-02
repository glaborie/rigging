# ADR-0021: Bearer tokens now, OIDC later, and no token means loopback only

**Status:** Accepted (extends ADR-0018)

## Context

ADR-0018 shipped the web view loopback-only with no auth and said exposing it
"would need its own ADR". v0.6 is that ADR. The service is read-only, but it
serves full run transcripts — tool arguments, model reasoning, customer data
after redaction. "Read-only" is not "harmless".

Two constraints shape the answer:

- **The browser cannot send headers on `EventSource`.** A token in the query
  string leaks into access logs, history and referrers.
- **A secret in `argv` is a secret in `ps`.** So the token comes from the
  environment, not a flag.

## Decision

- **One credential type: bearer token.** `RIGGING_SERVICE_TOKEN` (comma-separated
  to allow rotation) is compared with `hmac.compare_digest`. Any request to
  `/api/*` without a valid `Authorization: Bearer` gets `401` and
  `WWW-Authenticate: Bearer`.
- **The HTML shell is public, the data is not.** `GET /` serves static markup with
  no run data. The page asks for the token when the first API call returns 401 and
  keeps it in `sessionStorage`; every fetch and the stream send it as a header.
- **SSE uses `fetch` + `ReadableStream`, not `EventSource`.** That is what makes
  the header possible, and it keeps the token out of the URL.
- **No token means loopback only, enforced at startup.** `harness serve` refuses
  a non-loopback `--host` when no token is configured, with an explicit message
  rather than a silent open port. Loopback with no token stays supported: it is
  the local development case ADR-0018 described.
- **OIDC is deferred, not forgotten.** One read-only scope makes bearer tokens
  sufficient; OIDC (issuer discovery, JWKS, claims→scope mapping) is a deployment
  feature with its own threat model, and it would double the auth code for no
  gain on a single-operator dashboard.

## Consequences

The service can be exposed behind a reverse proxy with one static token, and
rotation is a restart with two values in the env var. Cost: no user identity, no
per-user audit trail, no token expiry (rotation is manual), and the token is a
shared secret — the same compromise as an API key. Anyone who needs SSO should
put an authenticating proxy in front until OIDC lands.

## Alternatives rejected

- **Query-string tokens**: leak in logs and history, and make every URL a secret.
- **Cookie session with a login form**: more moving parts (CSRF, expiry, signing)
  than a single read-only scope justifies.
- **`--token` CLI flag**: visible in `ps` and shell history.
- **Auth on `/` too**: then the page cannot prompt for the token it needs.
- **OIDC now**: real work, wrong milestone; no multi-user requirement exists.
