# Code Walkthrough — access-hub + access-guard

A fast, sequence-ordered tour of both apps. Follow it top-to-bottom to trace one
employee's access from enrollment to teardown. For the *why* read
[DESIGN.md](DESIGN.md); for the frozen wire contract read [PROTOCOL.md](PROTOCOL.md).
This doc is the *where* — every step points at `file:line`.

- **hub** = `apps/access_hub` — helpdesk/central. Not attacker-controlled → DocTypes fine.
- **guard** = `apps/access_guard` — installed on each client site. Attacker-controlled
  (ephemeral users get System Manager) → all integrity state is **file-backed**, never in the DB.
- **protocol** = `<app>/<app>/protocol.py`, vendored **byte-identical** into both apps. Stdlib-only.

Paths below are relative to each app's root (`access_hub/…` = hub, `access_guard/…` = guard).

---

## 0. The shared contract (read this first)

Both apps import the same `protocol.py`. Know these before reading anything else:

- `PV = 1` — protocol version; `check_pv()` (protocol.py:141) rejects mismatches with 426.
- `TRANSPORT_TOKEN_HEADER = "X-Access-Guard-Transport-Token"` (protocol.py:39) — a **custom**
  header chosen so Frappe's `validate_auth()` never touches it. This is why the guard wires
  **no** `auth_hooks` and **no** `override_whitelisted_methods` (see protocol.py:27-38).
- `transport_token_hash(token)` (protocol.py:77) — SHA-256 hex. Same hash is stored on the hub's
  Remote Site, embedded as the grant token's `aud`, and re-checked by the guard. The token binding.
- Grant claims: `CLAIM_EMP_NAME/EMP_EMAIL/JTI/PV` (protocol.py:43-46).
- Policy keys `POLICY_*` + `MODE_DENY`/`MODE_WARN` (protocol.py:50-61); `POLICY_DEFAULTS` (protocol.py:63)
  ships the User create/write/delete **deny** rule.
- Error taxonomy — every error carries `code` + `http_status` and an `.as_response()`:
  `ClientNotConfigured`(503) `TokenMalformed`(400) `TokenSignature`(401) `TokenExpired`(401)
  `TokenAudience`(403) `TokenReplay`(409) `ProtocolVersion`(426) `TransportAuth`(401) `PolicyDenied`(403).

Crypto is **PASETO v4.public (Ed25519)** via `pyseto`. Hub holds the private key and signs; guard
holds only the public key and verifies. Hub signs in `utils/crypto.py:sign_paseto` (crypto.py:24);
guard verifies in `tokens.py:verify_and_decode` (tokens.py:42).

---

## 1. Setup (one-time, hub side)

1. **Generate the keypair.** `bench access-hub-generate-keypair` →
   `commands/__init__.py:generate_keypair` (commands:48). Writes `access_hub_private_key` +
   `access_hub_public_key` to the hub's site_config (`_generate_paserk_keypair` commands:11).
   Idempotent unless `--force`.
2. **Seed default policy.** On install, `hooks.py:10` fires
   `access_hub_settings.after_install` (access_hub_settings.py:117) — idempotently seeds the
   default User-deny rule from `POLICY_DEFAULTS`.
3. **Register a client site.** Create a **Remote Site** doc. `remote_site.py:validate`
   (remote_site.py:14) autogenerates a transport token (`secrets.token_urlsafe(32)`), hashes it
   into `transport_token_hash`, and `on_update` (remote_site.py:48) wipes the plaintext from the DB
   after commit — the hash is all the hub keeps.
4. **Hand enrollment info to the client.** `bench access-hub-enroll-info` (commands:97) or the
   whitelisted `api.get_enrollment_info` (api.py:19) returns hub URL + public key + the **one-time**
   plaintext transport token. The guard operator drops these three into the client site's config
   (`CONF_HUB_URL/HUB_PUBLIC_KEY/TRANSPORT_TOKEN`, guard constants.py:5-8).

---

## 2. Request access (hub side — mint the grant)

Employee opens the **Request Access** desk page (`page/request_access/request_access.js`):

1. Page load → `api.list_remote_sites` (api.py:70) populates the picker. No secrets, no perm gate.
2. Click → `api.request_access(remote_site)` (api.py:86). Mints for `frappe.session.user`
   (**no** employee override; Administrator/Guest blocked). Returns `{redirect_url}`; JS does
   `window.location.href = …` (request_access.js:44).

Under `request_access`:
- `access_grant.py:create_and_mint(employee, remote_site)` (access_grant.py:25) — creates the
  Access Grant row (status Issued, uuid `jti`, `iat`/`exp` from `GRANT_TTL_SECONDS_DEFAULT` = 45 min,
  which is the **URL redemption deadline, not the session length**), sets `aud =
  site.transport_token_hash`, signs via `sign_paseto` (access_grant.py:62). Throws if the site has
  no hash.
- `access_grant.py:build_accept_url` (access_grant.py:66) → `{proto}://{host}:{port}/api/method/access_guard.api.accept?token=…`.

The employee's browser now carries the signed grant token to the client site.

---

## 3. Accept the grant (guard side — provision + login)

`access_guard/api.py:accept(token)` (api.py:29). Verification order is **fixed by PROTOCOL §3**:

1. `tokens.verify_and_decode` (tokens.py:42) does malformed → signature → pv → exp (with
   `CLOCK_SKEW_LEEWAY_SECONDS` = 60, constants.py:11). It does **not** check aud/jti.
2. `accept` then checks **aud** = `transport_token_hash(local transport token)` → `TokenAudienceError`.
3. `grants.mark_jti_used(jti)` (grants.py:28) — **atomic flock check-and-set**; raises
   `TokenReplayError` if the jti was already redeemed. This is the entire replay guarantee, and it
   is file-backed, independent of any DB row.
4. `_create_or_reuse_ephemeral_user` (api.py:103) — idempotent on email, appends the policy
   `name_suffix`, grants **all** roles (incl. System Manager). Runs as Administrator (api.py:68).
5. `grants.write_grant(jti, record)` (grants.py:49) — the grant file is now the **sole source of
   truth** for "this session is ephemeral".
6. `_start_session` (api.py:133) logs the user in via `LoginManager`; redirect to `/app`.
7. Best-effort: `hub_client.report_provisioning` (hub_client.py:52) and
   `logging_.log_session_event` (logging_.py:50).

`boot.py:boot_session` (boot.py:13) stamps `bootinfo.access_guard_ephemeral` (via
`find_grant_by_sid`, grants.py:101) so the browser shows the monitoring notice
(`public/js/monitoring_notice.js`) once per login.

---

## 4. Enforce, while the employee works (guard side)

Everything hinges on **"is `frappe.session.sid` in a grant file?"** — never a DB permission.
`session_guard.is_ephemeral_session` (session_guard.py:29) answers it.

Wired on `doc_events["*"]` (hooks.py:20):
- `validate` → `session_guard.validate` (session_guard.py:69): create if `doc.is_new()` else write.
- `before_submit`/`before_cancel`/`on_trash` → submit/cancel/delete (session_guard.py:76-84).

Each calls `_enforce_for_doc(doc, action)` (session_guard.py:33) → `policy.enforce` (policy.py:74):
- `MODE_DENY` → `frappe.throw` (raises `PolicyDeniedError`).
- `MODE_WARN` → `frappe.msgprint` + an after-commit `doc` log line.
- no matching rule → allow (`match_rule` first-match, policy.py:63).

Policy comes from `policy.get_policy` (policy.py:51) — the **file** merged over `POLICY_DEFAULTS`,
never the DB. Enforcement only applies when the session is ephemeral (grant-file keyed), so a normal
System Manager on the same site is untouched (test_policy_enforcement.py:57).

---

## 5. Log everything (guard side — out of the employee's reach)

Per-session append-only **JSONL**, one file per jti, under `site/private/access_guard/logs/`.
Deliberately *not* `frappe.logger`. All in `logging_.py`:

- `log_request` (logging_.py:57) — every whitelisted call. Wired via `request_hooks.before_request`
  ("attempt") / `after_request` ("ok"/"error") (request_hooks.py:31/42). Skips `READ_SKIP_METHODS`
  (constants.py:20, **fail-open**) when `log_reads` is off; truncates args to `max_key_size_bytes`.
- `log_doc_event` (logging_.py:83) — doc create/write/submit/cancel/delete diffs; size-exempt.
- `log_session_event` (logging_.py:50) — provision/login/logout/reap.
- **always_log** (`always_log.py`) — `on_update`/`on_trash` for `always_log_doctypes`; registers an
  after-commit diff log. This is the reliable channel that catches sensitive writes even when they
  arrive through a read-looking method.

All logging is a no-op for non-ephemeral sessions and fails open (logging never blocks the request).

---

## 6. Sync back to the hub (guard hourly → hub ingest)

Guard `sync.sync_hourly` (sync.py:39), gated on `policy.sync_enabled`, does four things via
`hub_client.py` (all best-effort, transport token in the custom header, never raises into a request):

1. `_push_all_logs` (sync.py:50) → `hub_client.push_logs` (hub_client.py:38); marks each jti acked.
2. `_announce` (sync.py:81) → `hub_client.announce` (hub_client.py:45).
3. `_pull_and_verify_policy` (sync.py:96) → `hub_client.pull_policy` (hub_client.py:60) +
   `policy.verify_and_store_policy` (policy.py:31) — re-verifies the hub signature before storing.
4. `_enforce_retention` (sync.py:106) — deletes acked logs past `push_grace_days`, everything past
   `retention_days`.

**Hub ingest side** — all in `api.py`, authenticated by the transport-token header, not login:
- `_authenticate_remote_site` (api.py:122) hashes the header token, matches
  `Remote Site.transport_token_hash`, else `TransportAuthError`.
- `ingest_endpoint` decorator (api.py:135) = `allow_guest` + `check_pv` + transport auth +
  error→HTTP mapping. It uses `@wraps` + `**kwargs` so Frappe still coerces args against the real
  signature (the regression the HTTP tests guard, test_protocol_flows.py `TestIngestEndpointsOverRealHttp`).
- `push_logs` (api.py:176) → Session Log `get_or_create` (session_log.py:12) + `append_lines` /
  `upsert_index` (session_log_file.py:41/59). Files land under
  `site/private/files/access_hub_logs/<host>/<jti>.jsonl` with a realpath traversal guard
  (`_safe_join`, session_log_file.py:21).
- `announce` (api.py:212) → `remote_site.announce` (remote_site.py:54) metadata upsert.
- `report_provisioning` (api.py:227) → a Comment on the Remote Site.
- `get_policy` (api.py:167) → `get_current_policy_token` (access_hub_settings.py:90).

**Policy push (the other direction):** editing **Access Hub Settings** → `on_update`
(access_hub_settings.py:32) rebuilds + signs the policy token and, if `sync_enabled`,
`push_policy_to_all_sites` (access_hub_settings.py:103) POSTs it to every Remote Site's
`access_guard.api.receive_policy` (guard api.py:153, verified by its own signature).

---

## 7. Teardown (guard side — remove the access)

- **Logout:** `on_logout` → `reaper.teardown_on_logout` (reaper.py:108) — logs `user_logout`, reaps
  the user if no sessions remain.
- **Idle reaper:** scheduler cron `*/15` → `reaper.reap_idle_sessions` (reaper.py:51). Effective
  expiry = min(policy, site) (`_effective_expiry_seconds`, reaper.py:24). When a grant's user has
  zero live sessions, `_reap_user` (reaper.py:78) deletes the **grant file before the User** to avoid
  `on_logout → teardown → _reap_user` recursion, then reports "reaped".
- Both are **namespace-filtered to grant-file users only** (`ephemeral_users`, grants.py:110;
  `_is_ephemeral_namespace_safe`, reaper.py:97) — a real user is never touched.

---

## File index (jump table)

### hub (`apps/access_hub`)
| File | Role |
|------|------|
| `access_hub/api.py` | whitelisted surface: enroll, request_access, log serving, + transport-auth ingest |
| `access_hub/protocol.py` | shared wire contract (vendored) |
| `access_hub/constants.py` | hub-only conf keys, grant TTL |
| `access_hub/hooks.py` | `after_install`, `require_type_annotated_api_methods` |
| `access_hub/commands/__init__.py` | `generate-keypair`, `enroll-info` CLI |
| `access_hub/utils/crypto.py` | `sign_paseto` with hub private key |
| `.../doctype/access_grant/access_grant.py` | `create_and_mint`, `build_accept_url` |
| `.../doctype/access_hub_settings/…py` | policy build/sign/push; `get_current_policy_token` |
| `.../doctype/access_policy_rule/…py` | child rule row (empty controller) |
| `.../doctype/remote_site/remote_site.py` | transport-token lifecycle; `announce` |
| `.../doctype/session_log/session_log.py` | session metadata by jti; `get_or_create` |
| `.../doctype/session_log_file/…py` | on-disk `.jsonl` + traversal guard |
| `.../page/request_access/request_access.js` | site-picker → mint → redirect |
| `access_hub/tests/test_protocol_flows.py` | mint/policy/ingest/serving + real-HTTP tests |

### guard (`apps/access_guard`)
| File | Role |
|------|------|
| `access_guard/api.py` | guest endpoints: `accept`, `receive_policy` |
| `access_guard/protocol.py` | shared wire contract (vendored) |
| `access_guard/constants.py` | guard-only conf keys, on-disk layout, read-skip list |
| `access_guard/hooks.py` | doc_events, scheduler, request hooks, boot, logout |
| `access_guard/boot.py` | stamps `ephemeral` flag into bootinfo |
| `access_guard/grants.py` | file-backed grants + replay store (flock) |
| `access_guard/policy.py` | verify/store policy; `enforce` deny/warn |
| `access_guard/tokens.py` | PASETO v4.public verify (steps 1-4) |
| `access_guard/session_guard.py` | doc_events enforcement, sid→ephemeral keyed |
| `access_guard/reaper.py` | idle reaper + logout teardown |
| `access_guard/sync.py` | hourly push/announce/pull-policy/retention |
| `access_guard/hub_client.py` | outbound guard→hub HTTP (custom header) |
| `access_guard/logging_.py` | per-session JSONL logging |
| `access_guard/always_log.py` | always-log doctype diff channel |
| `access_guard/request_hooks.py` | before/after request logging |
| `access_guard/config_utils.py` | conf validation, layout, atomic-write/flock/json |
| `access_guard/errors.py` | `AccessProtocolError` → wire response; `guest_api` decorator |
| `access_guard/public/js/monitoring_notice.js` | one-time recording notice |
| `access_guard/tests/` | accept, policy, reaper, logging, http-regression + `keys.py` minter |

---

## Five things that will bite you if you forget them

1. **Grant files, not DB, decide "ephemeral."** Every enforcement/logging/reap path keys on
   `find_grant_by_sid(frappe.session.sid)`. The DB is assumed hostile.
2. **Replay = one atomic flock check-and-set** (`grants.mark_jti_used`), file-backed, no DB row.
3. **The custom transport header is load-bearing** — it dodges Frappe's `validate_auth()`, which is
   why there are no `auth_hooks`. Don't "fix" it into an Authorization header.
4. **Whitelisted signatures stay plain/unparameterized** (see [CLAUDE.md](../CLAUDE.md)) — the
   ingest decorator's `@wraps`+`**kwargs` exists so Frappe's type coercion still runs; the real-HTTP
   tests exist because direct Python calls miss that path.
5. **`protocol.py` is byte-identical in both apps.** Change one → change the other in lockstep, or
   the wire contract silently diverges.
