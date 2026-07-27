# Ephemeral Access Provisioning — Design

Architecture and rationale for the two-app system that replaces standing System
Manager access on client sites with short-lived, provisioned-on-demand access.
This doc describes the system **as built**. The frozen wire contract lives in
[PROTOCOL.md](PROTOCOL.md) — token claims, endpoints, file/log schemas, error
codes — and is referenced, not restated, here. Step-by-step operator procedures
live in [OPERATIONS.md](OPERATIONS.md).

## Context

An ERPNext consultancy gives employees standing **System Manager** accounts on
hundreds of client sites. A few employees abused those permanent elevated
permissions, causing data breaches. The goal is to eliminate standing access: an
employee gets System Manager on a client site **only for a short window**,
provisioned on demand from the central helpdesk site and torn down automatically
— and everything they do is logged out of their reach.

The hard constraint: **no per-client base user + copied API key-pair.** Storing a
long-lived api_key/api_secret per client on the helpdesk is itself the standing,
copyable, breach-amplifying credential we are trying to remove. So the design
avoids any shared secret per client.

**App naming:**
- **access-hub** — helpdesk/hub-side app (this repo).
- **access-guard** — client-side app (sibling repo).
- **access-protocol** — the shared contract (docs + wire schema) between them.
  Vendored (a single `protocol.py` per app), not a third Frappe app.

**Target Frappe versions: v15, v16, and above.** Not v14 or below. All hooks
relied on below (`app_include_js`, `boot_session`, `doc_events`,
`scheduler_events`, `on_logout`) are stable across v15/v16.

Wire format is versioned (`pv`) but the system does **not** carry multi-version
compatibility — the hub serves exactly one `pv` and returns `ProtocolVersionError`
on mismatch. A few breaking upgrades on both sides are acceptable while this
matures.

## Foundational principles

These three are the spine of the security model. Everything else is mechanism in
service of them.

### No client-DB trust

An employee can become System Manager on a client site, so the **client DB is
attacker-controlled**. Nothing the security model depends on may live in the DB.
All integrity-critical state lives in **files** under
`site/private/access_guard/` (JSON config, per-session logfmt logs, grant/jti
state), out of desk reach. **Redis** and **site_config** are NOT reachable from
the UI or server scripts, so Redis is a *trusted-but-volatile* cache: files
remain authoritative for durability (Redis flushes/evicts → re-derive from
files), but Redis values need not be treated as attacker-forged.

### Constrained ephemeral user

The ephemeral User object unavoidably lives in the client DB. Rather than police
it after the fact, access-guard **prevents** it from performing the
meta-operations that would let it subvert the access system (default: deny
User create/edit/delete), so the reaper and guard can safely trust file-backed
state. See Access policy.

### File-backed integrity state

The grant file, used-jti store, and signed policy file are the authoritative
records for the reaper, session guard, and replay protection. They survive
downtime, dropped jobs, and Redis flushes. A client-side edit of an
already-pushed log line can't affect the hub copy. This is what lets the rest of
the system trust "is this an ephemeral session?" without consulting the DB.

---

## Provisioning: one path — asymmetric-signed token via browser

A single provisioning mechanism for all sites, FC or not. Trust flows one
direction via public-key crypto. The **helpdesk holds one private key**; each
**client stores only the helpdesk's public key** (non-secret, identical
everywhere, shipped in the app). No base user on clients; no secret copied to the
helpdesk. Exact token format (PASETO `v4.public`, Ed25519) and claim set are in
[PROTOCOL.md §0, §3](PROTOCOL.md).

Flow when an employee clicks **Request Access**:
1. The hub mints a **grant token** for `frappe.session.user` (see Enrollment for
   where the button lives). PASETO is chosen so the token format is
   unmisconfigurable — no `alg` header → no `alg:none` / key-confusion bugs.
2. The token carries **WHO + WHERE + WHEN only, no policy** — employee identity,
   `aud` (= SHA-256 of the client's transport token), `iat`, short `exp`, unique
   single-use `jti`, `pv`. No `roles`/`deny`/`warn` claims: the guard owns policy
   (see Access policy). `exp` is the **redemption deadline for the URL**, not the
   session lifetime — a captured-but-unclicked URL goes dead after `exp`.
3. The browser is redirected to the client's grant-acceptance URL with the token
   as a parameter (**HTTPS mandatory** so the token isn't sniffed).
4. access-guard verifies the token (order in [PROTOCOL.md §3](PROTOCOL.md):
   malformed → signature → pv → exp → aud → jti-replay), checking `aud ==
   SHA-256(local transport token)` and that the `jti` is unused (file-backed
   store). On success it:
   - writes a **grant file** — authoritative for the reaper and the session
     guard; records the **jti ↔ sid** mapping at login,
   - creates or reuses an ephemeral `User` (idempotent, keyed on email; see
     below),
   - assigns **all assignable roles on the target site** (every role Frappe's own
     `get_all_roles()` would show — every non-disabled, domain-active role except
     the automatic Guest / All / System User / Administrator — including any
     custom roles the client defined, not just System Manager),
   - sets `simultaneous_sessions` on the user at creation (multiple tabs share one
     sid in Frappe anyway),
   - starts a Frappe login session (the token doubles as a one-time login
     credential), then redirects to the site's configured default app
     (`get_default_path()`, falling back to `/app`) — where Frappe's own
     post-login would land a system user.

The token is self-contained and signed, so the client validates it **without
calling back** to the helpdesk — exactly what the browser-redirect (no
server-to-server) choice requires.

**Reuse only for guard-provisioned users.** Create-or-reuse is keyed on the grant
email, but reuse is safe **only for a user this guard itself provisioned**.
Ephemeral membership is grant-file-derived (`ephemeral_users()`), never a DB flag
— a User row exists iff a live grant names them, so that is the authoritative
test. If the email already belongs to a **standing (non-ephemeral) user**, accept
**refuses** (`EphemeralUserCollisionError`, no session started): handing a
standing account a fresh System Manager session is exactly the standing-access
abuse we are removing.

**`aud` = hashed transport token — rationale.** The transport token is already
required config on every client, so reusing it as the audience needs **zero extra
config** (no separate client-id field). The value the hub stores
(`H = SHA-256(transport_token)`) *is* the `aud`: the hub sets `aud = H` at mint
(never needs the plaintext), the client verifies `aud == SHA-256(its local
token)`, and a token minted for client A fails at client B. This is the "this is
for you" binding needed on the **server → user(browser) → client** courier path,
where the (possibly malicious) employee carries the token — cross-client replay
is what `aud` + single-use `jti` + short `exp` jointly defeat. `H` is exposed in
the browser token but the whole token is PASETO-signed (unforgeable);
user-defined transport tokens require a minimum length of 8.

---

## Enrollment (wiring a client to the hub)

One-time, low-stakes, separate from provisioning. A client's access-guard config
is a fixed three-value shape: **hub URL + helpdesk public key + per-client
transport token** ([PROTOCOL.md §1](PROTOCOL.md)). The admin installs
access-guard and applies those three values; the configs are validated at boot,
and if any is missing the guard refuses to accept tokens
(`ClientNotConfiguredError`). FC-site enrollment automation is a non-goal for now
(the `is_fc_site` flag on Remote Site marks such sites).

**Where enrollment values come from (hub side).** The two hub-wide values are
identical across every client, so they live on **Access Hub Settings**, not
per-site: `get_hub_enrollment_info` returns the hub URL + public key (gated on
Access Hub Settings read → System Manager). The **per-client transport token** is
shown exactly once, at creation or rotation, via a dialog on the Remote Site form
(**Update Token** → `rotate_transport_token`); only its `transport_token_hash` is
stored, so plaintext is unrecoverable afterward. An operator enrolling a new
client never needs to read `site_config.json` by hand on either side.

**Where "Request Access" lives.** There is no separate request-access desk page
and no per-site enrollment-info API — provisioning is driven straight off the
registry:
- a **Request Access** button on the **Remote Site form**, and
- a per-row **Request Access** button in the **Remote Site list** (the quick path
  from a Helpdesk ticket's filtered list).

Both call `access_hub.api.request_access`, which mints for `frappe.session.user`
(no `employee=` override — attribution is the point) and returns the guard accept
URL to redirect to.

**Read-gated request, write-gated rotation.** Remote Site grants **read-only** to
the roles **Desk User** and **Agent** (System Manager gets full access).
`request_access` enforces the caller's *read* permission on the specific Remote
Site — a site is requestable only if the caller's role can see it — and never
grants write, so it cannot rotate the token. The Update Token button is gated on
write permission, and `rotate_transport_token`'s `doc.save()` re-enforces that
server-side.

---

## Client → hub channel (outbound, one shared channel)

All client-initiated traffic goes to the hub's known URL over **HTTPS**, carrying
the **per-client transport token** in a custom header
(`X-Access-Guard-Transport-Token`) — low-privilege; reaches only hub ingest
endpoints; leak = post-as-that-one-client, nothing more. The hub stores the token
hashed at rest (SHA-256) and compares `hash(incoming)`; it never needs the
plaintext after registration. No HMAC/nonce — header-over-TLS is sufficient, and
TLS authenticates the hub's identity, so no separate "client verifies server"
mechanism is needed on this channel (the Ed25519-signed grant token already
authenticates the hub on the provisioning path). Endpoint list is in
[PROTOCOL.md §5](PROTOCOL.md).

**Why a custom header, not `Authorization: Bearer <token>`.** An earlier revision
used `Authorization`, which forced a global `auth_hooks` registration on the hub
purely to clear Frappe's own `validate_auth()` gate — that function intercepts any
2-part `Authorization` value *before* Frappe resolves which whitelisted method is
called, let alone runs any decorator wrapping it, and rejects with
`AuthenticationError` unless the header resolves to a real session via
OAuth/API-key/an `auth_hooks` entry. Moving the token to a custom header means
`validate_auth()` never looks at it, so **no hub-side registration point is
needed** — `ingest_endpoint` (in `api.py`) is the *only* place this token is ever
checked, end to end.

Channel uses:
- **Audit-log push** (scheduled, hourly): upload per-session log files. The file
  on disk is the durable source of truth, so delayed sync loses nothing.
- **Announce**: report existence + URL/version so the hub registry self-populates.
- **Provisioning-outcome reports**: report create/reap of an ephemeral user per
  `jti` (recorded as an Info comment on the Remote Site).
- **Policy pull** (hourly): fetch the signed global policy blob.

---

## Access policy: deny + warn, global, guard-enforced

**Posture: prevent the meta-operations that enable persistence, warn on other
sensitive ops, log everything.** Hub-side reconciliation is a non-goal for now, so
allowing User ops with no reconciliation would leave persistence entirely
uncaught. The guard **denies** the meta-ops instead.

**Policy shape (future-proof).**
- UI: authored on the hub as a Single DocType `Access Hub Settings`.
- Wire/file: a normalized, versioned JSON rule list, decoupled from the UI. A
  single `mode` field (`deny` / `warn`, extensible later to `require-approval`
  etc.) keeps the schema stable; actions use Frappe's canonical names
  (`create/write/delete/submit/cancel`). Exact policy-token format is in
  [PROTOCOL.md §4](PROTOCOL.md).
- **Default seed:** `User → deny [create, write, delete]`, seeded via an
  idempotent install step (guard-on-existence-before-insert).

**Policy is global** (not per-client). It is signed with the helpdesk key and
delivered two ways: **best-effort push on save** (fire-and-forget; failure OK —
reachable clients update fast) and **guaranteed hourly pull** by the guard
(covers firewalled/offline/new clients). The guard verifies the signature against
the public key and writes the policy to a **file** — never a client DocType (an
ephemeral System Manager would just edit "deny User" away).

**Enforcement mechanics.** The ephemeral user has System Manager, so DB
permissions can't gate anything — enforcement is entirely guard **code** keyed on
"is this an ephemeral session?" (read from the grant file):
- **deny** → block the op before write (`validate` / before-write hook),
- **warn** → a warn interstitial before proceeding, then log + allow.

**Prevention vs. detection boundary (accepted).** The guard is app code; an
attacker with server-script (Python) execution could sidestep hooks. Accepted —
mitigation is **detection via logging**, and the log sink is out of reach:
Log files under `site/private/` are not touchable by a server script absent
an actual exploit, and once a line is pushed to the hub it is immutable from the
client. Shorter push interval = smaller pre-immutability window; hourly is
acceptable given the file is already out of desk reach.

---

## Session & deprovisioning (must be bulletproof)

**Session lifetime = idle timeout = `min(AccessHubSettings.session_expiry, site
session_expiry)`.** `session_expiry` (default 6h) is an Access Hub Settings field;
the effective duration is the lower of it and the client site's own System
Settings value. It is **idle-based** (Frappe semantics). There is **no absolute
wall-clock cap** — an actively-used session refreshes and can persist up to the
idle limit.

- **Scheduled reaper (primary), `scheduler_event` every ~15 min:** for each
  ephemeral session, if idle beyond the effective expiry → kill the session
  (delete `tabSessions` rows + clear Redis); when an ephemeral user has **zero
  live sessions** → delete the user. Enforcing the *lower* bound ourselves matters
  because Frappe's `session_expiry` is global and may be higher than ours. Grant
  files are authoritative, so this survives downtime / dropped jobs / never-logout.
- **Session-tied cleanup (secondary), `on_logout` hook:** tear the user down
  immediately on logout (with logout reason recorded) so early-finished sessions
  don't linger to the next tick.
- **Reaper safety:** acts only on the ephemeral naming namespace; deletes the
  grant file before the user (so an `on_logout`→teardown recursion no-ops), and
  sessions before the user so an active tab can't outlive it; **never touches the
  client's own users.**
- **User identity:** idempotent create keyed on the grant email. `full_name` is
  generated, so the name suffix (default `consultant`) is appended to `last_name`
  → e.g. `John Doe (consultant)`.

---

## Logging

Rides the client→hub channel. Actions are written to files under
`site/private/access_guard/` (unreachable through Frappe), stamped with the grant
`jti`, pushed hourly. No per-site signing keys — the file-outside-the-DB boundary
already defends against the in-scope adversary.

**Mechanism — two files per session, plain append.** Do **not** use `frappe.logger`
(routes by module, wrong dir) and do **not** create one `logging.Logger` per sid
(handler/fd leak in long-lived workers). Instead **append logfmt lines to
per-session files, open-append-close per line** (traffic is a human doing manual
ops — low volume): `<jti>.log` for requests, `<jti>.doc.log` for doc changes — no
per-line type/jti/sid, since the filename already identifies both. `sid → jti` is
recorded in the grant file at login, so `before_request`/`after_request` read
`frappe.session.sid` → map to `jti` → append. Log-line schema is in
[PROTOCOL.md §7](PROTOCOL.md).

**What gets logged:**
- **Request stream — one line per call, written from `after_request`:** the skip
  decision keys off **HTTP verb safety, not an endpoint name**: safe verbs
  (GET/HEAD/OPTIONS — Frappe's `frappe.auth.SAFE_HTTP_METHODS`) are **skipped
  unless `log_reads` is on** (default off); unsafe verbs are **always logged**. A
  user's mutating action always arrives via an unsafe verb, so this is a reliable,
  version-proof signal — no per-endpoint denylist, no reliance on
  `frappe.db.transaction_writes`. The raw request body is captured (size-capped),
  not form_dict-derived args — meaningful for both JSON and non-JSON payloads.
- **Sensitive-doctype writes (always, in full):** `doc_events` `on_update` /
  `on_trash` on a configurable always-log doctype list (default: User, Server
  Script) register a `frappe.db.after_commit` callback that appends the doc's full
  state at commit time (not a diff) — **no commit → no log**. Exempt from the size
  cap. This is the reliable channel that *knows the doctype*, so it catches a
  sensitive write verb-independently, even if it rode a safe verb the request
  stream skipped.
- **Max key size (default 512 KB):** truncates the logged request body; doc-change
  lines are exempt.
- **Session lifecycle:** grant-accepted / login / logout (+ reason) has no local
  file — pushed immediately to the hub's Access Grant, see below.

---

## Hub-side storage & retention

The hub is **not** attacker-controlled (employees are not System Managers on the
helpdesk), so DocTypes are fine here — the no-DB-trust rule is client-only.

- **Session lifecycle → Access Grant:** start/end time and logout reason live on
  the same Access Grant row a request mints (no separate Session Log DocType) —
  one thing per session, and the guard pushes these fields immediately
  (`access_hub.api.report_session_event`, always on, no policy opt-out) rather
  than waiting for the hourly log sync.
- **Request/doc logs → files + index (hybrid):** store the pushed per-session logs
  as files (`base/<remote_site>/jti.log`, `jti.doc.log`) — a DocType table would
  bloat across hundreds of clients — plus a lightweight **Session Log File** index
  DocType, one row per (jti, log_kind), for search/browse.
- **Safe serving:** never accept a client-supplied path. Serve by DocType id →
  resolve to a canonical path server-side → normalize and assert the real path is
  within `base_dir` (traversal guard). Gated on the Session Log File doctype's own
  read permission (System Manager). Download-only in v1 — line-range viewing is
  deferred to v2.
- **Retention:** hub-configured, default 365 days, polled by the guard; a
  session's local file is deleted when **(acked by hub AND local age > grace
  [default ~7 days]) OR age > max (365d)**. The grace buffer protects against
  hub-side loss before durable backup and leaves a debugging window.

---

## Remote Site doctype (client registry)

The hub's registry of client sites. Key modeling decisions:
- **`autoname: "hash"`** — the document ID is an opaque hash, not the host. Hosts
  change (rename, protocol/port change) without churning the primary key or
  breaking references (grants, session logs, log-file paths keyed on
  `remote_site`).
- **`host` is the title field** (`title_field`, `show_title_field_in_link`) and is
  **mutable** — the human-readable identity, editable independent of the ID.
- **`is_fc_site` (Check)** marks Frappe-Cloud-managed sites (enrollment automation
  is a non-goal for now); there is no `enrollment_type` field.
- **`transport_token_hash`** (read-only) stores only the SHA-256 — plaintext is
  shown once at creation/rotation and never persisted.
- `port` (default 443), `protocol` (default https) — set at enrollment, never
  updated by the guard (v2: a reliable self-reported announce/heartbeat).
- **Permissions:** System Manager full; **Desk User** and **Agent** read-only.
- With Helpdesk installed, a system-generated `customer` custom field (Link → HD
  Customer) links a client site to its helpdesk customer — see Install.

---

## Install / migrate setup

Orchestrated in `access_hub/setup.py`, wired via `hooks.py`
(`after_install` / `after_migrate`); everything idempotent.

**`after_install` makes the hub usable out of the box:**
- **auto-generates the signing keypair** (`ensure_keypair`) — private key to
  `site_config.json`, public key persisted alongside it; never in the DB,
- **seeds the default policy** (`seed_default_policy` — `User → deny [create,
  write, delete]`),
- then runs the same app-adaptation step as migrate.

**`after_migrate` (and `after_install`) — Helpdesk integration, only when
Helpdesk is installed:**
- creates a **custom field `Remote Site.customer`** (Link → HD Customer, optional
  convenience link),
- upserts an **HD Form Script** ("Access Hub - Ticket Sites Button") that adds an
  **Access Sites** button to HD Tickets. The button shows only when the ticket has
  a customer *and* the caller can read Remote Site; clicking routes to the
  customer-filtered Remote Site list. The server (`ticket_sites_url`) owns both the
  permission check and the target URL, so the form script hardcodes no desk path
  and no policy.

Both integration steps no-op cleanly on sites without Helpdesk.

---

## Frappe primitives reused (not reinvented)

- `frappe.new_doc` for User creation; roles via `add_roles(*get_all_roles())`.
- `frappe.local.login_manager` / `frappe.auth` to establish a session after
  verification (pattern from `ldap_settings`: assign `login_manager.user`, run
  `post_login()`).
- `hooks.py`: `scheduler_events` (reaper, sync), `on_logout`, `doc_events`,
  `app_include_js`, `boot_session`, `after_install` / `after_migrate`.
- `@frappe.whitelist(allow_guest=True)` for the acceptance endpoint.
- `frappe.db.after_commit` for commit-gated sensitive logging.
- `frappe.auth.SAFE_HTTP_METHODS` for the request-log verb-safety skip.
- `frappe.utils.get_url_to_list` / `get_default_path` for building desk URLs (no
  hardcoded `/app`).
- Secrets via `frappe.conf` (site_config.json), never DocType fields.

---

## Monitoring notice on client sites

An unintrusive, enabled-by-default notice ("your activity is being monitored"),
static text. Rendered version-robustly via `app_include_js` (a slim bar), gated on
"am I ephemeral?" via a `boot_session` flag derived from the grant file (not a DB
flag). Both hooks are stable v15/v16; avoid touching desk DOM internals.

---

## Security notes / edge cases

- **Short `exp` + single-use `jti`** bound a leaked redirect URL: useless after
  `exp` or first use.
- **Clock skew:** small leeway allowed on `exp`.
- **Key rotation:** clients hold only the public key → rotation = ship a new
  public key (support a small valid set during transition). No per-client secret
  churn.
- **Compromised helpdesk** still needs the private key to forge grants; there is
  no vault of per-client api_secrets to steal — the central design goal.
- **Standing-user collision:** accept refuses to start an ephemeral session for an
  email that is already a standing user (`EphemeralUserCollisionError`).
- **HTTPS mandatory** on the redirect and the transport channel.
- **Transport token entropy:** autogenerated tokens are CSPRNG (32 bytes);
  admin-defined tokens require min length 8. `aud` exposes only
  `SHA-256(token)`; the grant token is signed (unforgeable).
- **Never delete non-ephemeral users** — reaper filters strictly on the ephemeral
  namespace.
- **No wall-clock session cap** — an active session persists to the idle limit.

---

## Future / non-goals (deliberately out of scope for now)

- Approvals (auto-grant vs require-approval per client).
- Hub-side reconciliation / persistence detection + flagging + notifications.
- Early/remote revoke (hub → client push) — managers force-logout manually
  meanwhile.
- FC enrollment add-on (auto-register + write config).
- Per-client policy overrides (policy is global today).
- Ephemeral email generation (today: reuse the grant email).
- "Disallow simultaneous sessions" toggle (clamp to single session).
- Absolute max session duration / force-logout independent of idle expiry.
- HMAC + timestamp/nonce on the transport channel.
- Multi-version protocol tolerance (hub serves N old guard versions).
- Standalone access-protocol repo + submodule.
