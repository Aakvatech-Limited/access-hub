# access-protocol — frozen wire contract (v1, pv=1)

This is the **single source of truth** shared by access-hub and access-guard. Both apps
implement against it independently; nothing here is negotiable at build time. The machine-readable
half of this spec is `protocol.py` (token claims, the policy document, the error taxonomy),
vendored **byte-identical** into both apps at `<app>/<app>/protocol.py`. Constants used by only one
side (site_config.json keys, on-disk layout, etc.) live in that app's own `<app>/<app>/constants.py`
instead — not vendored, since only one side ever needs them.

If you are building one app, treat the *other* side of every exchange below as fixed. Integration
is verified after both apps are independently green.

---

## 0. Crypto primitive

Everything signed uses **PASETO `v4.public` (Ed25519)** via `pyseto`. The hub holds the private
key (`k4.secret`), the guard holds only the public key (`k4.public`). Two things are signed:
1. the **grant token** (hub → browser → guard),
2. the **policy token** (hub → guard).

No other signature scheme, no symmetric crypto, no HMAC in v1. `pv` (=`1`) is a claim in every
signed artifact and a field in every JSON payload; a mismatch → `ProtocolVersionError` (HTTP 426).

### Timestamps are UTC-only

Every timestamp on the wire (`iat`, `exp`) and in any **file** either side persists (grant records'
`issued_at`, the push-ack `acked_at`, log-line `ts=`) is ISO-8601 **carrying an explicit `+00:00`**.
Mint via `protocol.utc_iso()`, read via `protocol.parse_iso()`, which **rejects a naive value**
rather than assuming UTC.

This is load-bearing, not cosmetic. `frappe.utils.now_datetime()` is naive *System Settings* local
time; a receiver that assumes naive means UTC shifts every expiry by the emitter's UTC offset — so a
120s grant token lived ~5.5h on an IST hub, and would have been born already-expired on a US one.
DocType Datetime **fields** keep Frappe's own naive-system-local convention (that's what the desk
renders), so the two must never be compared directly.

## 1. Configuration (`site_config.json`)

**Guard site** (all three required; missing any → `ClientNotConfiguredError`, refuse tokens):
- `access_guard_hub_url` — base URL of the hub.
- `access_guard_hub_public_key` — hub's PASETO public key (`k4.public...`).
- `access_guard_transport_token` — per-client bearer, **min length 8**.

**Hub site:**
- `access_hub_private_key` — PASETO secret (`k4.secret...`), auto-generated on install (or via the
  `bench access-hub-generate-keypair` command).
- `access_hub_public_key` — the matching `k4.public...`, persisted on the hub itself. This backs the
  hub's enrollment-help surface, which hands a client admin the three guard values above without
  anyone reading `site_config.json` by hand:
  - **Hub-wide values** (`access_guard_hub_url` + `access_guard_hub_public_key`, identical for
    every client) are pushed onto the **Access Hub Settings** form by its `onload`
    (`set_onload("hub_enrollment_info", ...)`) and rendered as a copyable field by
    `access_hub_settings.js` — no separate API call.
  - The **per-site `access_guard_transport_token`** is shown **once**, at creation or rotation,
    via a dialog on the **Remote Site** form (button **"Update Token"** → `rotate_transport_token`).
    It is never stored plaintext — only `transport_token_hash` lives on the Remote Site. There is
    no per-site `get_enrollment_info`.

## 2. `aud` / transport-token binding

`transport_token_hash(token) = sha256(token).hexdigest()`. This one value is used three ways:
- Hub stores it on the Remote Site as `transport_token_hash` (the token itself is never stored
  plaintext after registration).
- It is the grant token's **`aud`** claim (binds the token to one client → anti cross-client replay).
- The guard verifies `aud == transport_token_hash(local transport token)`.

## 3. Grant token (hub → browser → guard)

PASETO `v4.public`, signed with the hub private key. Claims:

| claim | source | notes |
|---|---|---|
| `user_first_name` | user first name | custom |
| `user_middle_name` | user middle name | custom; may be null |
| `user_last_name` | user last name | custom |
| `user_email` | user email | custom; becomes the ephemeral user id |
| `aud` | `transport_token_hash(client token)` | registered; verified on accept |
| `iat` | now | registered (ISO-8601, **UTC-aware**) |
| `exp` | `iat + ~2m` | registered; **URL redemption deadline**, not session length |
| `jti` | uuid4 hex | custom; single-use |
| `pv` | `1` | custom |

**Delivery:** the hub redirects the employee's browser (HTTPS) to
`GET {guard_url}/api/method/access_guard.api.accept?token=<paseto>`.

**Guard accept — verify in this order**, mapping failures to the named errors:
1. parse → `TokenMalformedError`
2. verify signature w/ public key → `TokenSignatureError`
3. `check_pv(claims.pv)` → `ProtocolVersionError`
4. `exp` (allow `CLOCK_SKEW_LEEWAY_SECONDS`) → `TokenExpiredError`
5. `aud == transport_token_hash(local token)` → `TokenAudienceError`
6. `jti` not in used set → `TokenReplayError`; then mark used.
7. resolve ephemeral user for `user_email`: create if absent, or reuse **only** if the email already
   belongs to an ephemeral user. If it belongs to a **standing (non-ephemeral)** existing user →
   `EphemeralUserCollisionError`, no session started. Ephemeral membership is grant-file-derived
   (`ephemeral_users()` = every `user` named in a grant file), never a DB flag.

On success: write grant file, create/reuse ephemeral user, start session, redirect to
`get_default_path()` (the configured default app for the logged-in user) or `/app`.

## 4. Policy token (hub → guard)

The policy is delivered as a **PASETO `v4.public` token** whose claims are the policy object +
`iat` + `pv`. Guard verifies the signature (this *is* the authentication — no transport token
needed for a pushed policy) and writes `{policy, verified_at}` to the policy file. The token itself
is deliberately **not** kept — only its verified contents.

Policy object example:

```json
{
  "pv": 1,
  "rules": [{"doctype": "User", "actions": ["create","write","delete"], "mode": "deny"}],
  "session_expiry_seconds": 21600,
  "always_log_doctypes": ["User", "Server Script"],
  "max_key_size_bytes": 524288,
  "log_reads": false,
  "log_sync_enabled": true,
  "retention_days": 365,
  "push_grace_days": 7,
  "name_suffix": "consultant",
  "access_disabled": false,
  "blacklisted_emails": [],
  "disallow_simultaneous_sessions": false
}
```

`mode ∈ {deny, warn}`; `actions ⊂ {create,write,delete,submit,cancel,rename}`.
**Delivery:** policy push on save + policy pull on the guard's hourly check-in are both
unconditional (not configurable in v1), so `access_disabled`/`blacklisted_emails` always reach a
client quickly. `log_sync_enabled` only gates the rest of that hourly check-in (log push, retention).
`disallow_simultaneous_sessions` (default off): when on, each ephemeral user is capped to one
active session (`User.simultaneous_sessions = 1`, applied on every accept() - both fresh
provisioning and reuse - so a policy change takes effect the next time that user's grant is
accepted, not just at first creation); off uses a large-but-finite cap (100) since Frappe's own
session-clearing logic (`get_sessions_to_clear`, frappe/sessions.py) treats a falsy
`simultaneous_sessions` as 1, not "unlimited".

## 5. Endpoints

### Hub (authenticated by transport token: `X-Access-Guard-Transport-Token: <raw token>`)
**Deliberately not the `Authorization` header** — see `protocol.py::TRANSPORT_TOKEN_HEADER` for why
(Frappe's `validate_auth()` intercepts any 2-part `Authorization` value before a whitelisted method
runs at all, forcing an unnecessary global `auth_hooks` registration; a custom header sidesteps that
gate entirely, so `ingest_endpoint` is the only place this token is ever checked).
Hub computes `transport_token_hash(received)` and matches a Remote Site → else `TransportAuthError`.
The matched Remote Site (not any payload field) is what identifies the client to each endpoint.

| method | payload | returns |
|---|---|---|
| `access_hub.api.get_policy` | — | `{ "policy_token": "<paseto>" }` |
| `access_hub.api.push_logs` | `{ jti, log_kind: "request"\|"doc", lines }` | `{ ok, received_lines }` |
| `access_hub.api.report_session_event` | `{ jti, event: "login"\|"logout", reason }` | `{ ok }` |

`report_session_event` is called immediately (not batched with the hourly
`push_logs`) — always on, no policy opt-out — so the hub's Access Grant reflects
session start/end in near-real-time.

**Authentication is not authorization.** The transport token proves *which site
is calling*; it does not by itself say *whose records that caller may touch*. So
both jti-bearing endpoints additionally require the `jti` to be a grant **of the
authenticated Remote Site** (`{jti, remote_site}`, not a by-name lookup — Access
Grant is autonamed `field:jti`, so filtering by name alone would ignore the
site). Without it, any enrolled client — every one of which legitimately holds a
token — could end or re-activate another site's grant with an arbitrary
`logout_reason`, or file logs under another site's jti and pick up that grant's
`user_email`.

**`push_logs` size cap.** `lines` is capped at `MAX_PUSH_BYTES` (8 MiB) per call,
measured **in bytes on both sides**, so one client cannot fill the issuer's disk.
The constant lives in the shared `protocol.py` rather than hub-only for a reason:
the hub rejects an oversized body outright, so a client that doesn't know the
number cannot split at the right place — it re-sends the same rejected payload
every cycle, never advances its pushed offset, and eventually loses the file to
retention. A conforming client therefore **chunks on line boundaries** and
advances its offset per accepted chunk, so a mid-sequence failure keeps what
landed and resumes rather than duplicating the tail. A single line longer than the
cap has no boundary to split on, so it is truncated to fit while the offset still
advances past the whole line — a truncated line beats the whole file stalling on it
forever.

### Guard
| method | auth | payload | returns |
|---|---|---|---|
| `access_guard.api.accept` (allow_guest) | the grant token | `?token=<paseto>` | 302 → default app path (`get_default_path()` or `/app`) |
| `access_guard.api.receive_policy` (allow_guest) | the policy token itself | `{ token }` | `{ ok }` |

All error responses use `AccessProtocolError.as_response()` →
`{ "error": { "code", "pv", "message" } }` with the class's `http_status`.

## 6. Grant file (`site/private/access_guard/grants.json`)

Authoritative for the reaper and the session guard. Never in the DB. One shared
file, keyed by jti (`{jti: record, ...}`) — not one file per grant.

```json
{
  "jti": "...", "pv": 1,
  "user_name": "...", "user_email": "...",
  "user": "john.doe@example.com",     // ephemeral user id (== user_email in v1)
  "sid": "...",                        // written already-set, after login (maps session to jti)
  "issued_at": "iso"
}
```

The reaper's idle-expiry always reads the *current* policy.session_expiry_seconds
live (never a value stamped at accept-time), so a policy change takes effect for
already-issued grants too. Frappe's own site-wide session_expiry is enforced
separately, per-request.

"Is this session ephemeral?" → `frappe.session.sid` appears as a `sid` in some grant file.
"Which users may the reaper touch?" → only `user` values present in grant files (namespace filter,
`ephemeral_users()`).

## 7. Log line schema (`site/private/access_guard/logs/<jti>.request.log`, `<jti>.doc.log`)

Two files per session (jti) — no per-line type/jti/sid, since the filename already identifies
both. logfmt lines (`key=value`, space-separated; a value containing whitespace is JSON-quoted).
Session lifecycle (login/logout) has no local file — see `report_session_event` (§5).

```
# <jti>.request.log — one line per whitelisted call, written from after_request
ts=<iso> path=frappe.client.save http_method=POST status=ok code=200 params={"key":"...","...":"each top-level key truncated to max_key_size independently"} body={"...":"parsed JSON body, if any, or the raw string if not JSON"}

# <jti>.doc.log — one line per doc event, size-exempt
ts=<iso> event=create doctype=User name=... doc={"...":"full doc state"}
ts=<iso> event=write doctype=User name=... diff={"changed":[[field,old,new],...],"added":[...],"removed":[...],"row_changed":[...]}
ts=<iso> event=rename doctype=ToDo name=<new_name> doc={"old_name":"...","new_name":"..."}
ts=<iso> event=denied:<action> doctype=User name=... doc=... | diff=...
```

Request logging keys the skip decision off the **HTTP verb**, not the endpoint name: **safe verbs**
(GET/HEAD/OPTIONS, from `frappe.auth.SAFE_HTTP_METHODS`) are skipped unless `log_reads` is on;
**unsafe verbs are always logged**. `params` (query args + form-encoded fields) and `body` (the
request body — parsed as JSON when the request is JSON, else the raw string) are separate fields,
never merged into one dict — folding `body` into `params` would risk it colliding with an actual
query/form field literally named `body`. Both are read straight from the request object, not
`frappe.form_dict` (mutable, handler-writable, not trustworthy for an audit log).

Doc-event logging: `create`/`cancel`/`delete`/`rename` log the full doc (or `{old_name, new_name}`
for rename — nothing else changes); `write`/`submit` log a diff against the pre-save state instead
(`Version.get_diff`, falling back to the full doc if the diff itself fails). A denied attempt logs
immediately as `denied:<action>` (nothing commits on a deny, so it can't ride `after_commit` like
the rest) — diff or full doc, same rule as the allowed case, since the pre-save state is available
regardless of whether the operation was ultimately denied. Sensitive-doctype writes are caught
verb-independently by the always-log doc channel (`always_log_doctypes`), which now also covers
submit/cancel/rename, not just write/delete.

## 8. Version / errors

`pv = 1`. Error codes + HTTP statuses are defined once in `protocol.py` as the `AccessProtocolError`
subclass hierarchy. v1 supports exactly `pv=1`; anything else is `ProtocolVersionError`, not a
degraded parse.

| class | `code` | HTTP |
|---|---|---|
| `AccessProtocolError` (base) | `access_protocol_error` | 400 |
| `ClientNotConfiguredError` | `client_not_configured` | 503 |
| `TokenMalformedError` | `token_malformed` | 400 |
| `TokenSignatureError` | `token_bad_signature` | 401 |
| `TokenExpiredError` | `token_expired` | 401 |
| `TokenAudienceError` | `token_wrong_audience` | 403 |
| `TokenReplayError` | `token_replayed` | 409 |
| `ProtocolVersionError` | `protocol_version_mismatch` | 426 |
| `TransportAuthError` | `transport_auth_failed` | 401 |
| `PolicyDeniedError` | `policy_denied` | 403 |
| `EphemeralUserCollisionError` | `ephemeral_user_collision` | 409 |
