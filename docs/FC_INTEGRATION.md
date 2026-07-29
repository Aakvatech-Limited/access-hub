# Frappe Cloud integration — design

**Status: draft.** Both former `TODO(fc-api)` holes are now closed — the press request/response
shapes are captured in [FC_API.md](FC_API.md). Remaining opens are listed in §7 and none are
blocking.

Inspiration and prior art: `fc_mis` (same bench, `apps/fc_mis`) already talks to FC with a team API
key. This reuses its client shape (`FrappeClient` subclass, `X-Press-Team` header, `Password`
fieldtype for the secret) rather than inventing a second one.

---

## 1. What this adds

Today enrollment is entirely manual: an operator creates a **Remote Site**, copies the three config
values into the client's `site_config.json`, and ticks the site live. That stays supported and is
the fallback whenever any of the below is off or fails.

FC integration automates two steps for **FC-hosted sites in one team**:
1. **Discovery** — find the team's sites that have `access_guard` installed and register them as
   Remote Sites (access **disabled**, `is_fc_site` **checked**).
2. **Enrollment** — write the guard's three config values to those sites and enable access.

Each is a separate checkbox, so discovery can run without enrollment.

## 2. Settings — Access Hub Settings, new "Frappe Cloud" section

Single doctype, new section at the end (after Enrollment). Three checkboxes + three fields, as
specified:

| field | type | default | notes |
|---|---|---|---|
| `enable_fc_integration` | Check | **0** | Global switch; everything below is `depends_on` it. |
| `fc_team_name` | Data | — | `X-Press-Team` header value. `mandatory_depends_on` the global switch. |
| `fc_api_key` | Data | — | `mandatory_depends_on` the global switch. |
| `fc_api_secret` | **Password** | — | Read via `get_password()`; never rendered. `mandatory_depends_on` the global switch. |
| `fc_auto_pull_sites` | Check | **1** | Daily discovery (§4). |
| `fc_auto_enroll_sites` | Check | **1** | Auto-config + enable access (§5). |
| `fc_sync_now` | Button | — | Run discovery immediately (§4). |

**The global switch is a real field, and unticking it keeps the credentials.** This is the whole
point of not using fc_mis's derived `enabled = bool(api_key and api_secret and team_name)`: with a
derived flag, the only way to pause FC integration is to delete the API secret and re-enter it later.
Here it's a pause switch — untick, credentials stay put, nothing runs; retick, it resumes.

Correctness is enforced in the other direction instead, so "enabled" can never mean "enabled but
unconfigured":
- `mandatory_depends_on: "eval:doc.enable_fc_integration"` on all three credential fields — the
  framework refuses to save an enabled-but-blank config and marks the fields required in the UI the
  moment the box is ticked.
- `validate()` re-checks server-side (a `db_set`/API write bypasses client-side mandatory).

**Off means off, everywhere.** The switch is checked in three places, not just the UI: the scheduled
job returns early, the Sync Now button refuses, and `FrappeCloudClient.__init__` throws. The last is
the backstop — no code path can talk to press with the integration disabled, however it was reached.

The two inner checkboxes default **on**, per spec: once an operator has deliberately enabled FC
integration and supplied credentials, the useful behaviour is the automatic one. They remain
individually switchable, and are inert while the global switch is off.

**Team is single-valued for now.** A future multi-team world turns `fc_team_name` into a child
table. To avoid a backfill then, Remote Site also gets an **`fc_team`** (Data, read-only) — **derived,
not supplied**: `RemoteSite.validate()` stamps it from this setting on every save, so it cannot drift
from the configured team and a row always records which team it came from.

## 3. FC client

`access_hub/fc.py` — a `FrappeClient` subclass mirroring `fc_mis.utils.FrappeCloudClient`:

```python
class FrappeCloudClient(FrappeClient):
    def __init__(self, settings=None): ...        # throws if integration disabled/unconfigured
    def fetch_all_sites(self): ...                # get_list Site -> name, host_name (limit!)
    def installed_apps(self, site_id): ...        # get_list Site App, parent=site_id (N calls)
    def guard_enrollment(self, site_id): ...      # get_list Site Config -> {hub_key, token_hash}
    def update_config(self, site_id, config): ... # run_doc_method update_config (merges)
    def delete_config(self, site_id, key): ...    # run_doc_method delete_config
```

Every call is `post_api(method, json=payload)` — press answers a GET with a bare `AttributeError`.
Pagination is `limit`/`start`; the desk's `limit_page_length` is silently ignored (FC_API "Gotchas").

Base URL `https://cloud.frappe.io`, `X-Press-Team: <fc_team_name>`, key/secret from settings. Not
vendored from fc_mis (different app, no shared dependency) — deliberately duplicated, since the two
apps' needs diverge past the constructor.

Every method takes the press **site ID** (`Site.name`, `*.frappe.cloud`), never `host_name` — see
§4. Endpoint shapes and response quirks: [FC_API.md](FC_API.md).

**Never log an `update_config`/`delete_config` response body.** press echoes the site's entire merged
config back, secrets included (FC_API §6). Log endpoint + site + submitted key *names* only. This
diverges deliberately from fc_mis, which dumps `frappe.as_json(response)` into Error Log.

## 4. Discovery — `fc_auto_pull_sites`

**Daily** scheduled job (`scheduler_events.daily_long`; the hub has no `scheduler_events` yet, this is
the first), plus a **Sync Now** button for on-demand runs (see below).

Per run:
1. `fetch_all_sites()` → `[{name, host_name}, ...]` for the team: one `get_list` on `Site` with
   `limit: 0` (**not** the desk-style `limit_page_length`, which press ignores — you'd silently get
   20 rows; FC_API §1). **No status filter** — FC status says nothing about whether access should be
   issuable (that's `access_disabled` + guard policy), so we neither filter nor store it.
2. Keep only sites with **`access_guard` installed**. One `Site App` call **per site**: apps aren't in
   the site list, and batching is blocked by `SiteApp.get_list_query`, which `get_doc`s
   `filters["parent"]` as a single docname (FC_API §2). Discovery is therefore `1 + N` — measured
   **35.8s for 108 sites** (0.33s/call). Runs in the **`long` queue**, so this is a non-issue.
3. **Skip any site already enrolled against a different hub.** Read the site's
   `access_guard_hub_public_key` (FC_API §3) and compare it to ours:
   - **absent** → unconfigured, adopt it;
   - **ours** → adopt it (this hub restored from backup, or the row was deleted by hand);
   - **someone else's** → *ignore the site entirely*, and log a count.

   This is not hypothetical: on the live team, `freemis-av.frappe.cloud` is enrolled against the
   production hub's key, so a dev hub's sweep correctly reports it as `other_hub` and leaves it alone.

   Without this, one team's hub would silently hijack sites belonging to another: `update_config`
   merges, so writing our three keys would overwrite the other hub's public key and transport token,
   and every grant that hub issues would start failing `TokenAudienceError`. A skipped site is logged
   rather than passed over quietly, since an operator waiting for it to appear needs to know why it
   didn't. This is the one config *value* we read; it's a public key, so reading it leaks nothing.
4. For each kept site, upsert a Remote Site — matched on **`fc_site_id`** first, then, if none, on an
   **unclaimed row whose `host` equals either `host_name` or `name`** (adoption, see below):
   - **new row** → `host` = **`host_name`**, `fc_site_id` = **`name`**, `port` 443, `protocol` https,
     `is_fc_site` 1, **`access_disabled` 1**. A transport token is autogenerated by
     `RemoteSite.validate()` as usual, but its plaintext is *not* surfaced (nobody is watching a
     scheduled job) — enrollment (§5) is what delivers it.
   - **adopted row** (hand-made before FC integration existed) → gains `fc_site_id` + `is_fc_site` and
     follows `host_name`, but **keeps its `access_disabled` as-is**, and its token unless enrollment
     re-pushes. Adoption is not consent to enable or disable a site an operator already set up.
     Only rows with an empty `fc_site_id` are adoptable, so one FC site can never steal another's
     registration; a genuine host collision surfaces as a per-site failure (host is `unique`) rather
     than a silent reassignment.
   - **existing row** → the sync owns `is_fc_site` and `host` (a custom domain can change on FC; the
     row should follow it), and `fc_team` is re-stamped from the settings by `RemoteSite.validate()`.
     It must **not** touch `access_disabled` (the operator's kill switch) or re-issue the token.
5. Sites no longer in the listing (dropped, migrated off, moved teams) are **released, not deleted** —
   grants, session logs and log files reference Remote Site. Their FC fields are cleared
   (`is_fc_site`, `fc_site_id`, `fc_team`, `fc_enrolled_at`) and `access_disabled` is left as the
   operator set it. Since `is_fc_site` is read-only, this sweep is the *only* thing that can un-tick
   it — without it a departed row would demand an `fc_site_id` forever.

   A site that **uninstalls access_guard** is released the same way: it's recorded as seen only after
   the app check, so it stops being an FC site here — correct, since without the guard this hub can't
   serve it.

   "Departed" means absent from the listing, tracked as a set of every site the listing accounted for —
   including ones we deliberately didn't adopt (another hub's), since those are still on FC. One guard
   remains: the release is skipped entirely when the listing came back empty or a site errored, because
   a bad response must not read as "everything left FC".

### `fc_site_id` — why `host` can't be the key

press has two identifiers and they are **not** interchangeable (FC_API §1):
- `Site.name` — the press site ID (`primaroyal-av.frappe.cloud`). Immutable, and **every** press call
  (`Site App`, `Site Config`, `update_config`) addresses a site by it.
- `Site.host_name` — the custom domain actually served (`erp.palefjord.com`). This is what a browser
  must reach, so it's what the guard accept URL needs, i.e. Remote Site's `host`. **Mutable.**

So Remote Site needs **`fc_site_id`** (Data, read-only, unique-when-set) in addition to `host`.
Matching the upsert on `host` would break the moment a client sets or changes a custom domain: the
sweep wouldn't recognise the existing row and would insert a duplicate (or trip `host`'s unique
constraint). Matching on `fc_site_id` is stable and is also the handle enrollment needs.

This is the second field beyond the spec's 3+3, and like `fc_enrolled_at` (§6) it's load-bearing
rather than nice-to-have.

### Schedule — daily, plus a Sync Now button

`daily_long` (the hub's first `scheduler_events` entry), matching fc_mis's choice for a comparable
full-team sweep. Discovery detects a rare event — someone installing access_guard on a site — so an
hourly `1 + N` sweep would spend 24× the third-party requests to shorten a delay nobody is waiting on.
Anyone who *is* waiting presses the button.

**Sync Now** — a `Button` field on the settings form calling a whitelisted
`access_hub.fc.sync_sites_now()`:
- refuses unless `enable_fc_integration` (and re-checks credentials);
- enqueues the same function the scheduler calls, in the `long` queue — a 36s sweep must not run
  inline in a request;
- `frappe.msgprint(..., alert=True)` that it's queued, so the operator isn't left guessing.

This is also the credential smoke test: an operator who just entered a key/secret shouldn't wait until
tomorrow to learn they typo'd it. Same reason fc_mis has one.

Error handling follows fc_mis: catch **per site**, `frappe.log_error`, `continue` — one bad site
never aborts the batch. The whole job is wrapped so a dead FC API logs once and returns.

Idempotent by construction: matching on `fc_site_id` (immutable, unique-when-set) means a re-run
updates rather than duplicates, even if the site's custom domain changed since.

### Reconciling an already-configured site

A site may already carry guard config for **this** hub — hub restored from backup, Remote Site row
recreated by hand, or config edited on the site. The hub can never recover a transport token's
plaintext (only its hash is ever stored), so discovery compares hashes and there are exactly two
honest outcomes:

| site's state | `fc_enrolled_at` | effect |
|---|---|---|
| token hash **matches** the row | stamped (if not already) | already enrolled; enrollment skips it, so a working token is never rotated |
| hash differs, config partial, or config **removed** | **cleared** | enrollment re-pushes a fresh token — the only route back to a known state |

The clearing half is what makes this self-healing. `fc_enrolled_at` is what enrollment filters on, so
a stale stamp on a site whose config was deleted would mean the site is never repaired: its guard
would fail every ingest call with `TransportAuthError` and the hub would never notice. Re-checked
every sweep, so it recovers within a day.

Three deliberate restraints:
- **Reconciliation never touches `access_disabled`.** Recognising or repairing a site is not consent
  to switch it on — only a newly discovered site's first enrollment enables access.
- **The deployed token is never adopted.** Copying its hash onto the row would authenticate fine and
  save a press write, but leaves the hub holding a token it cannot reproduce — rotation needs a
  plaintext, and press only returns one for FC sites. Re-pushing keeps a single rule for FC and
  hand-enrolled sites alike, and guarantees the hub always knows what it issued.
- **Repair is gated on `fc_auto_enroll_sites`.** That switch (or the global one, or the site's own
  `access_disabled`) is how an operator says "leave this site's config alone". With it off the pass is
  **pull-only**: sites are registered with access disabled, and no client's config is written or
  repaired — discovery records what it found and changes nothing.

Reconciliation never touches `access_disabled`: proving a token still works is not a reason to
re-enable a site someone deliberately switched off.

## 5. Enrollment — `fc_auto_enroll_sites`

For each Remote Site that is `is_fc_site` and not yet enrolled:
1. Rotate/get a transport token (plaintext held only in memory for this call).
2. Submit the three guard values to press for that site's `site_config.json`, addressed by
   `fc_site_id`: `access_guard_hub_url`, `access_guard_hub_public_key`,
   `access_guard_transport_token` ([PROTOCOL.md §1](PROTOCOL.md)).
3. Stamp `fc_enrolled_at`. **Enable access only for sites discovery just created** — which
   republishes that site's policy token (already wired, see the per-site kill switch). A repair or an
   adopted row keeps whatever access state it already had. `sync_sites` returns the rows it created
   and `run_sync` hands that list to enrollment, so no state on the doctype is needed.

Mechanism (FC_API §4): `run_doc_method` → `update_config` with only our three keys.

**It merges, so no read-modify-write.** press folds the submitted keys into the site's existing
config and sends the whole thing to its agent — the client's `encryption_key`, `host_name`,
`installed_apps` etc. are preserved, so we never need to read the config first.

**press accepting the config is authoritative.** It applies the write via an `Agent Job`
(`status: "Undelivered"` in the response), so there is a short window before the site actually has the
file — but press owns that write, and treating its acceptance as truth is what makes enablement
immediate instead of waiting a day for the next sweep. Two things keep that honest:
- the write either lands or the next sweep's reconciliation finds our keys absent and re-pushes
  (§4's reconciliation table), so a lost write self-heals;
- a *failed* submission never stamps anything — `fc_enrolled_at` is only written after press has
  accepted (see "Failure is not silent" below).

The rejected alternative was waiting for the guard's first authenticated call. It's stronger evidence,
but it put FC bookkeeping on the ingest path: a guard authenticating only proves it holds a token we
issued, which is equally true of a hand-enrolled site, and it made `get_policy` answerable for FC
state it has nothing to do with.

**Why this must stay `site_config.json`.** The alternative — having the guard persist hub URL, public
key and transport token in its own DB — is rejected. The client DB is attacker-controlled in this
threat model; a client admin who can rewrite the hub public key can forge grant tokens for their own
site. Config lives in files, full stop. This is the same reason there is no guard-side "enroll me"
HTTP endpoint: it would have to be authenticated by the very token it is provisioning.

**Ordering matters.** Token first, config second, stamp + enable last — and all three only after
press accepted the push. A failed submission must never leave the hub expecting a token the site
never received.

**Failure is not silent.** A site whose config write fails stays `access_disabled = 1` and gets an
Error Log entry. The operator's manual copy-config flow remains available for it.

### Update Token on an FC site

The **Update Token** button (`rotate_transport_token`) is FC-aware. When this hub can reach the site
(`fc.can_push_config`: FC site, has an `fc_site_id`, auto-enrol on, integration configured) it pushes
the new token to press **synchronously** and returns `{"pushed_to_fc": 1}`; the form shows a "token
applied" message instead of a config to copy. Otherwise it returns `{"config": {...}}` and the copy
dialog appears as before.

Three details worth keeping:
- **Push before save.** If press refuses, nothing is stored, so the old token keeps working rather
  than the hub expecting one the site never received.
- **The copy dialog is keyed on delivery, not on `is_fc_site`.** `_apply_token` flags the plaintext as
  undelivered and `fc.push_guard_config` clears that flag, so `onload` surfaces it exactly when nobody
  delivered it. Gating on `is_fc_site` was a bug: an FC site with auto-enrol *off* got neither a push
  nor a dialog, leaving its token unrecoverable.
- **Both paths return the updated doc** via `frappe.desk.form.save.send_updated_docs`, since both
  rotate `transport_token_hash` — no client-side `reload_doc()` round-trip.

Both enrollment paths build their three values from `RemoteSite.rotate_and_build_config()`, so the
operator-copy and FC-push flows can't drift apart.

## 6. Interaction with the per-site kill switch

`Remote Site.access_disabled` is the operator's switch. Automation must never fight it:
- discovery creates rows **disabled** and never re-enables;
- enrollment enables **once**, on a successful first enrollment;
- neither ever flips a site *back* on after an operator disabled it. Enrollment therefore needs a
  "has been enrolled" marker rather than inferring from `access_disabled` — otherwise the next
  hourly run re-enables everything an operator just turned off.
  *Design consequence:* Remote Site needs **`fc_enrolled_at`** (Datetime, read-only). Set when
  enrollment is *confirmed* (§5); enrollment skips any row that has it. Load-bearing — without it,
  `fc_auto_enroll_sites` is a kill-switch-override loop.

## 6a. Remote Site — "Frappe Cloud" section

The FC fields live in their own **Frappe Cloud** section on Remote Site, and **`is_fc_site` moves out
of the top block into it** as that section's own gate — the same show/hide relationship the settings
section has, one level down:

| field | type | visibility | why |
|---|---|---|---|
| `is_fc_site` | Check, **read-only** | always | Section gate. Sync-owned: set on discovery, cleared when the site stops being listed (§4.5). Relocated, not new. |
| `fc_site_id` | Data, read-only | `depends_on: is_fc_site` | press addresses sites by `Site.name`; `host` is the mutable custom domain. Upsert key. (§4) |
| `fc_team` | Data, read-only | `depends_on: is_fc_site` | Which team discovered the row, so a future team-table needs no backfill. (§2) |
| `fc_enrolled_at` | Datetime, read-only | `depends_on: is_fc_site` | Distinguishes "never enrolled" from "operator disabled it". Prevents the override loop. (§6) |

`host = host_name`, `fc_site_id = name` — the mapping is fixed and non-negotiable (FC_API §1).

All three new fields are **read-only**: they're sync-owned facts about FC, not operator inputs. An
operator's inputs on an FC site remain `access_disabled` (the kill switch) and the transport token.

Relocating `is_fc_site` is a pure field-order/section change — the fieldname is unchanged, so existing
rows and the `docs/DESIGN.md` reference to it stay valid.

## 7. Open questions

**Settled since the draft:**
- Discovery schedule — **`daily_long`** + a **Sync Now** button (changed from hourly; a rare event
  doesn't warrant 24× the requests, and the button covers anyone actually waiting).
- Global switch is a real field; unticking it **keeps credentials** and pauses everything.
- Batching the app filter — **not possible**: `SiteApp.get_list_query` resolves `filters["parent"]`
  with `get_doc`, so only one parent per call, and `Bench App` reports bench apps rather than the
  site's own (FC_API §2).
- Remote Site FC section with `is_fc_site` as its gate; `host = host_name`, `fc_site_id = name`.
- Leaked hub private key — acknowledged, rotation is yours to do outside this design.

- Enrollment confirmation — **press accepting `update_config` is authoritative** (§5). The
  guard's-first-check-in alternative was rejected for putting FC bookkeeping on the ingest path.
- Enabling access happens **only** on a newly discovered site's first enrollment. Repairs, adopted
  rows and reconciliation never touch `access_disabled`, so a site an operator disabled stays
  disabled through every sweep.

**Still open:**
1. Cosmetic: sites that vanish from FC are logged, not flagged (§4.5).

## 8. Verified live

Every press call this design depends on was executed against the real API on 2026-07-29
(`fc-mis.localhost` credentials, team `info@aakvatech.com`):

- **Discovery** — 108 sites listed in one call; per-site app filter over all 108 in ~31s; correctly
  identified the one site running access-guard (`freemis-av.frappe.cloud`).
- **End-to-end sync** — an early run (before the hub-key rule) created the row with
  `host = host_name` (`freedom-mis.aakvaerp.com`), the right `fc_site_id`, `fc_team` and
  `access_disabled = 1`. With the rule in place the same site is now correctly reported as
  `other_hub` and skipped, because it belongs to the production hub — the rule working on real data.
- **Idempotency** — a second consecutive sweep produced identical counts and no duplicate rows.
- **`update_config`** — wrote `testing` to `support-av.frappe.cloud`; re-read confirmed the merge
  preserved every pre-existing key.
- **`delete_config`** — removed `testing` only; site left as found.

Response *shapes* are the one thing not to trust: live `update_config`/`delete_config` returned
neither the `docs` nor the `Agent Job` the dashboard capture showed, while still performing the write.
Hence enrollment treats a non-exception from `update_config` as success (§5) rather than parsing it.
