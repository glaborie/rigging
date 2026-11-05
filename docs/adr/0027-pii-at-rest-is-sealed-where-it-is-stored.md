# ADR-0027: PII at rest is sealed where it is stored

**Status:** Accepted (v0.8)

**Amends:** ADR-0026 ("never written to disk"), ADR-0025 (the sentinel map's scope).

## Context

ADR-0025 and ADR-0026 put the harness's pseudonymisation story in good shape on
the model path: the model, the event log, the eval suite and every exporter only
ever see `[EMAIL_1]`. Two gaps remained, both found by running the demo rather
than by reading it:

1. **What a tool writes down is plaintext.** `notify_customer` stores the address
   it mailed in `demo.db.notifications.contact`, and `submit_basket` stores the
   contact on an order. After the process exits, that row is the *only* copy of
   the address in the system — see session `s_f824c5a9410f`, where a supplied
   contact survived only as `[EMAIL_1]`.
2. **The sentinel map dies with the process.** In `s_f824c5a9410f` a contact
   given in one turn was gone after a restart, so the next turn could not restore
   it. ADR-0026 accepted that (memory only, "the log stays a dead end"), but the
   cost is a real conversation that cannot continue — the customer has to give
   the address again.

Encryption does not fix the first gap by itself (the tool still has to write
something), and it does not fix the second unless the map is allowed to persist —
which ADR-0026 forbade. Both are storage decisions, so they get one decision.

## Decision

**A single AES-256-GCM primitive (`rigging/crypto.py`) seals PII where it is
stored, and an encrypted per-session vault (`rigging/vault.py`) persists sentinel
maps.**

- Values are sealed as `enc:v1:<base64(nonce||ciphertext)>`, a random 96-bit nonce
  per value, and an AAD binding the ciphertext to where it belongs
  (`orders.contact:<order id>`, `notifications.contact`, `sentinel:<session id>`).
- The key is 32 bytes from `RIGGING_PII_KEY` (base64 or hex), shared by both
  users of the primitive. An empty key means no sealing and no vault — the
  pre-ADR-0027 behaviour — and nothing is ever written in plaintext *by the
  sealing code paths*.
- `open_sealed` returns a value without the prefix unchanged, so a seeded or
  pre-existing database keeps working.
- The vault stores one file per session, named `sha256(session_id)`, mode `0600`,
  written atomically (tmp + replace), pruned by the same TTL as the in-memory map
  (default 3600 s). A vault that cannot be read or written warns once and degrades
  to memory-only: lost placeholders must never fail a run.
- The demo's tools use the primitive: `submit_basket` and `notify_customer` seal
  on write, `lookup_order` opens on read. The guardrail still pseudonymises the
  result before the model sees it, so the model path is unchanged.

## What this buys, and what it does not

The key lives in `.env`, next to the process. So this protects a *file* that
leaks — a backup, a disk image, an attached or accidentally committed database, a
snapshot of a volume — and nothing against a compromised host or a leaked key.
That is the honest claim, and it is why `docs/threat-model.md` states it as a
claim with limits rather than as a guarantee.

AAD binds a ciphertext to a column (or a session), so a value moved between
columns does not open. Two values in the same column of the same table can be
swapped undetected, because the row id is unknown before the insert; binding that
too would cost a second write per row for a threat the demo does not have.

## Migration

Two sources of plaintext survive the feature shipping, and one script closes them:

- **Fixture rows** from a `make seed` that ran without a key: `seed.build_db` now
  seals contacts when `RIGGING_PII_KEY` is set. Without a key it still writes
  plaintext, so `make seed` keeps working with no setup — and the bytes stay
  deterministic, which a keyed seed gives up (random nonces) while the data does
  not.
- **Rows written before the key existed**: `scripts/seal_pii.py <db> --vacuum`
  rewrites them with the AAD the readers use (`orders.contact:<order id>`,
  `notifications.contact`), skipping anything already sealed. `--vacuum` matters:
  after an `UPDATE`, SQLite keeps the old bytes in free pages, so without it "the
  file no longer contains the address" is nearly true rather than true.

A wrong key in either path is unrecoverable by construction; the script's own
help says to take a copy first.

## Reading a value back

Sealing that nobody can undo is a one-way function with extra steps, and the
operational cost — `sqlite3 demo.db` no longer shows a contact — has to come with
a way to look one up. `harness pii open <db>` is that way:

```
$ uv run harness pii open examples/b2b-support/demo.db --table notifications --limit 3
column                   row          state       value
notifications.contact    1            opened      anna.meier@nordwind-ag.ch
notifications.contact    2            opened      anna.meier@nordwind-ag.ch
notifications.contact    3            opened      joe@wh.com

3 contact(s): 3 sealed, 0 plaintext
```

Run against the same file with a different key in `.env`, every row changes state
and no value appears:

```
orders.contact           ORD-1001     unreadable  (cannot open: wrong key, or the ciphertext was altered)
```

- It is a command rather than a script because it is a routine diagnostic; the
  *migration* stays in `scripts/seal_pii.py`, because a rewrite that cannot be
  undone should not sit one keystroke from `--help`.
- It reports `state` next to the value, so "this row was sealed" and "this row
  opens with my key" are not conflated: a legacy plaintext row says `plaintext`,
  a value sealed under another key says `unreadable` and never prints nothing.
- One unreadable row does not hide its neighbours, and a table missing from an
  older database is skipped rather than fatal.
- The locations and their AAD templates live in `rigging/pii_store.py`, which
  both the command and the migration script read. A reader and a writer that
  disagree about the AAD produce a value nobody can open, so the binding is
  written down once, next to the tables it applies to.

## Costs

- **One-shot runs do not vault.** `assemble()` runs before the session exists, and
  a `harness run` or an eval case deliberately keeps a per-run map: persisting PII
  for a finished run would add a disk copy with no second turn to spend it on. The
  vault is for callers that pass a session — web chat, the REPL, subagents.
- A new optional dependency (`cryptography`, the `[vault]` extra). `make install`
  and CI pull it so the path is exercised, but the base install does not need it.
- `demo.db` is no longer readable with bare `sqlite3` for the sealed columns: a
  support engineer needs the key to read a contact. That is the point, and it is
  a real operational cost — one the demo's own debugging habits have to absorb.
- An encrypted blob in a row that *looks* like a contact breaks "grep the DB" and
  any downstream export that assumed plaintext.
- Legacy rows stay plaintext until rewritten; the claim is "values the tools
  write are sealed", not "every byte in every database is".

## Alternatives rejected

- **SQLCipher for the whole database.** Encrypts the event log too, needs a
  build-time variant of SQLite, and makes the file unreadable to `sqlite3` — which
  every debugging path in this repo (and `harness runs`) depends on. It also
  protects nothing the field encryption does not, against the threat that matters
  here.
- **OS/volume encryption only, documented (no code).** Still the right control
  for the host and the backup, and the threat model says so — but it does nothing
  for a database file that is copied, and it would leave the portfolio piece with
  a non-claim where a small, tested mechanism is possible.
- **Deriving the key from a passphrase (PBKDF2/Argon2).** Adds a KDF choice and a
  password the harness would have to prompt for, in a service that starts
  unattended. A 32-byte random key in `.env` is easier to rotate and impossible to
  brute-force by guessing.
- **Persisting the sentinel map *inside* `demo.db`.** One database, but the vault
  is harness-level and the demo database is one tool's storage; keeping the vault
  keyed by session id means a subagent and its parent can share it without the
  demo knowing.
