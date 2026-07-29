# Frappe Cloud (press) API — reference

What access_hub's FC integration needs from press, 2026-07-29. Three sources, in order of authority:
**press source** at `/home/snv/Park/dev/apps/press`, **live calls** against `https://cloud.frappe.io`
(team `info@aakvatech.com`, 108 sites), and the original dashboard curl capture. See
[FC_INTEGRATION.md](FC_INTEGRATION.md) for the design that consumes this.

**Not vendored, not a contract.** Unlike `protocol.py`, press is someone else's API and can change
under us. Treat every response as untrusted shape: read keys defensively, log what arrived when a
key is missing (`fc_mis` does exactly this — catch per site, log, continue).

**Read the source before believing the docs.** Several behaviours here are non-obvious and only
explicable from `press/api/client.py` and the per-doctype `get_list_query` overrides — the "Reading
the source" section at the bottom says where to look.

## Auth

Dashboard traffic is cookie/CSRF-authenticated; **we use an API key/secret instead**, exactly as
`fc_mis.utils.FrappeCloudClient` does. Two headers matter, everything else in the captured curls
(`Cookie`, `X-Frappe-CSRF-Token`, `sentry-*`, `baggage`, `Sec-*`, `Referer`, `DNT`, `Priority`, `TE`,
`User-Agent`) is browser noise and is **not** reproduced:

```
Authorization: token <api_key>:<api_secret>
X-Press-Team: info@aakvatech.com          # FC_TEAM_HEADER; the team whose sites you're acting on
Content-Type: application/json
```

`X-Press-Team` is not optional — it scopes every call below to a team. `FrappeClient` supplies the
`Authorization` header from `api_key`/`api_secret`; we set the team header ourselves.

## Generic endpoints

press exposes a generic client layer rather than one endpoint per operation, plus one purpose-built
site-listing endpoint:

| endpoint | purpose |
|---|---|
| `press.api.site.all` | list the team's sites, fixed field set (what fc_mis uses; see §1) |
| `press.api.client.get_list` | list a child table for **one** parent (`Site App`, `Site Config`) |
| `press.api.client.run_doc_method` | call a whitelisted method on one doc (`update_config`, `delete_config`) |

Responses are wrapped in Frappe's usual `{"message": ...}` envelope; `run_doc_method` additionally
returns a `docs` array (`fc_mis`'s `_run_doc_method` returns `docs[0]`).

### Must be POST with a JSON body

`FrappeClient.get_api()` sends a **GET with query params** and press answers every one of the calls
below with a bare `AttributeError`. Use `post_api(method, json=payload)` — a POST with a JSON body,
matching the dashboard. Params go as **native JSON types** (a real list for `fields`, a real dict for
`filters`), not `json.dumps`'d strings.

This is a mistake worth knowing about: the failure mode is an opaque `AttributeError` with no hint
that the transport is what's wrong.

### Pagination params are `limit` / `start`

From the signature in `press/api/client.py`:

```python
def get_list(doctype, fields=None, filters=None, order_by=None,
             start: int = 0, limit: int = 20, parent=None, debug=False)
```

`limit` **defaults to 20**. The dashboard's `limit_page_length` / `limit_start` are Frappe-desk names
that press does not accept — pass them and they're ignored, silently leaving you with 20 rows and no
error. Use `limit`, and `start` to page. Verified against a 108-site team: `limit: 0` → all 108
(`0` means no limit), `limit: 500` → 108, `limit: 20` → 20, `start: 100, limit: 20` → the last 8.

### Unknown fields and filters are dropped silently, not rejected

`validate_fields` / `validate_filters` keep only what `is_allowed_field` permits and **discard the
rest without complaint**. A field is allowed if it's in the controller's `dashboard_fields`, a
`default_fields`/`child_table_fields` member, a permitted `link.field` path, or a permitted
`{table: [fields]}` dict. So a typo'd or non-dashboard field just vanishes from the response — this
is why requests here look partly ignored (e.g. `host_name` missing while unrequested `bench` and
`status` appear, the latter two because `Site.get_list_query` selects/derives them itself).

### Verified 2026-07-29

Everything below was executed against the live API from `fc-mis.localhost`, not inferred from the
dashboard capture. Where behaviour contradicts the capture, the live result wins and is called out.

---

## 1. List sites

Either works; both return all **108** sites for this team in one call.

**`POST press.api.client.get_list`** — preferred, since you choose the fields:

```json
{"doctype": "Site", "fields": ["name", "host_name"], "limit": 0, "order_by": "creation desc"}
```

**`POST press.api.site.all`** with body `{}` — what fc_mis uses. No field control; returns a fixed
18-key payload (`name`, `host_name`, `status`, `creation`, `title`, `team`, `bench`, `group`,
`cluster`, `plan`, `version`, `public`, `tags`, `trial_end_date`, `server_region_info`,
`current_cpu_usage`, `current_database_usage`, `current_disk_usage`).

There is **no 20-row cap** — that was `limit`'s default being hit because the desk-style
`limit_page_length` was silently ignored. Pass `limit` (`0` = all).

Returns a flat list (already unwrapped by `FrappeClient.post_process`):

```json
[{"name": "primaroyal-av.frappe.cloud",  "host_name": "primaroyal-av.frappe.cloud"},
 {"name": "aim14-av.frappe.cloud",       "host_name": "erp.aimsteel.co.tz"},
 {"name": "palefjord-av.c.frappe.cloud", "host_name": "erp.palefjord.com"}]
```

We read **`name` and `host_name` only**. No app info is present in either shape (§2).

`get_list` also returns `bench` and `status` whether you ask or not — `Site.get_list_query` selects
`bench` itself and rewrites `status` to `"Update Available"` for benches with pending updates.

**We do not filter or track site status.** `status` is in the payload; we ignore it. A site's FC status
says nothing about whether an ephemeral-access grant should be issued (that's the hub's own
`access_disabled` + the guard's policy), so carrying it would be state we never read. Same for the
plan/cluster/group/usage fields the dashboard needs for its own UI.

### `name` vs `host_name` — the distinction that matters

- **`name`** is press's site ID (`*.frappe.cloud`). **Every other API call keys off this**, never off
  `host_name`.
- **`host_name`** is the custom domain the site is actually served on, and is what a user's browser
  hits. It differs from `name` whenever a custom domain is set (see `erp.palefjord.com` above), and
  equals `name` when none is.

Consequence for us: a Remote Site's `host` (used to build the guard accept URL, so it must be the
domain a browser can reach) should come from **`host_name`**, while the FC site ID must be stored
**separately** to address press later. They are not interchangeable and `host_name` is mutable.

---

## 2. List a site's installed apps — one call per site

`POST press.api.client.get_list` on the **`Site App`** child table.

Installed apps are **not** in the site list (§1) and **not** on the `Site` doc (`press.api.client.get`
returns 40+ keys, none of them `apps`). So this is one call per site.

```json
{
  "doctype": "Site App",
  "fields": ["app"],
  "filters": {"parenttype": "Site", "parent": "primaroyal-av.frappe.cloud"},
  "limit": 0
}
```

`parent` is the site's **`name`** (§1), never `host_name`.

Response rows carry far more than requested (repository, branch, hash, the full release commit
message, marketplace plans, subscription…) — all noise. The one field we need is `app`:

```json
[{"app": "frappe"}, {"app": "erpnext"}, {"app": "hrms"}, {"app": "csf_tz"}, {"app": "staff_loans"}]
```

**The guard test** is `"access_guard" in {row["app"] for row in rows}`.

### Batching is not possible — the `Site App` controller forbids it

The reason is one override, not press's permission layer. `SiteApp.get_list_query`
(`press/press/doctype/site_app/site_app.py`) **discards the query builder entirely**:

```python
@staticmethod
def get_list_query(query, filters=None, **list_args):
    site = cstr(filters.get("parent", "")) if filters else None
    if not site:
        return None
    site_doc = frappe.get_doc("Site", site)          # <-- one docname, hard stop
    installed_apps = get_installed_apps(site_doc, filters)
```

`cstr(["in", [...]])` stringifies the list, so `get_doc` looks for a Site literally named
`"['in', ['primaroyal-av…', …]]"` → `DoesNotExistError`. Nothing about the filter reaches SQL.

Note this is *not* the generic child-table path being restrictive. `ownership.scope_by_parent` would
have handled `in` fine — it joins the parent table and filters by team rather than resolving a
docname. The `Site App` controller pre-empts it.

Everything tried, and why each fails:

| attempt | result |
|---|---|
| `"parent": ["in", [names]]` | `DoesNotExistError` — the `cstr`/`get_doc` above |
| `filters` as list-of-lists `[["parent","in",[…]]]` | `AttributeError` — press expects a dict |
| `"parent": ["in", […]]` without `parenttype` | `ValidationError` — child reads require both |
| filter by `app`, no `parent` | `ValidationError` — same requirement |
| `fields: [{"apps": ["app"]}]` on `Site` | dropped: `apps` isn't in `Site.dashboard_fields` |
| `fields: ["apps.app as app"]` on `Site` | dropped, same reason |

**`Bench App` is not a substitute.** It looks batchable at first glance (its `get_list_query` builds a
real query), but it still pins `.where(BenchApp.parent == parent)` to one bench — and more
importantly a bench's app list is the *image*, not what a given site has installed, so it would report
apps a site doesn't actually have. Wrong data, not just an awkward query.

### Cost is a non-issue

Measured over a **full 108-site sweep**: 35.8s total, **0.33s per call**, run sequentially. In a
`long` queue that's nothing. No batching, no incremental "only unregistered sites" caching — the
simple hourly re-sweep is affordable and has no staleness to reason about.

---

## 3. Read a site's config

`POST press.api.client.get_list` on the **`Site Config`** child table.

```json
{
  "doctype": "Site Config",
  "fields": ["key", "value", "type"],
  "filters": {"parenttype": "Site", "parent": "primaroyal-av.frappe.cloud"},
  "limit": 0
}
```

```json
[{"key": "access_hub_private_key", "value": "k4.secret.…", "type": "String"},
 {"key": "encryption_key",         "value": "*******",     "type": "String"},
 {"key": "installed_apps",         "value": "[\"frappe\", \"wiki\", \"helpdesk\", \"access_hub\"]"},
 {"key": "testing",                "value": "access-hub-probe"}]
```

**It returns the site's real config, secrets included.** Verified live against
`support-av.frappe.cloud`: the response carried `access_hub_private_key` in full plaintext alongside
`installed_apps` and a masked `encryption_key`. (An earlier draft of this doc claimed it showed only
press-managed keys — wrong; the dashboard capture just happened to be a site with one key set.)

Two consequences:
- Useful as an enrollment check (are our three `access_guard_*` keys present?).
- **Never log this response either.** Same rule as §4 — site + key *names* only, never values.
  Masking is inconsistent: `encryption_key` came back `*******` while the PASETO secret did not.

We still never need read-modify-write — `update_config` merges (§4).

---

## 4. Set config keys (enrollment)

`POST press.api.client.run_doc_method`, method **`update_config`**.

```json
{
  "dt": "Site",
  "dn": "support-av.frappe.cloud",
  "method": "update_config",
  "args": {"config": {"access_guard_hub_url": "https://hub.example.com",
                      "access_guard_hub_public_key": "k4.public...",
                      "access_guard_transport_token": "..."}}
}
```

### It merges — it does not replace (verified live)

Tested on `support-av.frappe.cloud`: submitted `{"testing": "access-hub-probe"}` alone, then re-read
§3. Every pre-existing key (`access_hub_private_key`, `access_hub_public_key`, `encryption_key`,
`installed_apps`) was still there, with `testing` added. Corroborated by the `Agent Job`'s
`request_data` in the dashboard capture, which shows press sending the site's **entire merged config**
to its agent:

```json
"request_data": "{\"config\": {\"access_hub_private_key\": \"...\", \"encryption_key\": \"...\",
                  \"domains\": [...], \"host_name\": \"...\", \"installed_apps\": [...],
                  \"plan_limit\": {...}, \"rate_limit\": {...}}, \"remove\": [\"testing\"]}"
```

So we submit **only our three keys** and press preserves the client's other config. No read endpoint
needed, no clobbering.

`delete_config` (`args: {"key": "testing"}`) likewise removed just that key and left the rest intact.

### Applied asynchronously — success is not confirmation

The `Agent Job` in the dashboard capture came back `status: "Undelivered"` with
`job_type: "Update Site Configuration"` — press **queues** a config write, it doesn't perform one
inline. **Enrollment must not enable access off the back of this response** (FC_INTEGRATION §5). The
honest confirmation is the guard successfully authenticating against the hub.

**Don't depend on the response shape at all.** Live, `update_config` returned neither `docs` nor
`message` (an empty dict after `post_process`), and `delete_config` returned no `message.status`
either — while the browser capture showed both a full `Site` doc and an `Agent Job`. The write
happened in both cases regardless. Treat a non-exception as "submitted", and verify by outcome (§3, or
the guard checking in), never by parsing this.

## 5. Remove a config key

`POST press.api.client.run_doc_method`, method **`delete_config`**. One key per call:

```json
{"dt": "Site", "dn": "support-av.frappe.cloud", "method": "delete_config", "args": {"key": "testing"}}
```

Same async caveat as §4. Useful for un-enrolling a site (drop the three `access_guard_*` keys).
Verified live: removed only `testing`, leaving every other key intact.

---

## 6. Security note — press hands out the hub's private key

`access_hub_private_key` (`k4.secret...`) for `support-av.frappe.cloud` came back in **plaintext**
from two different places: the `request_data` of the captured `delete_config` response, and a plain
§3 `Site Config` read (verified live). It is not masked, though `encryption_key` on the same site is.

Two things follow:

1. **That key should be rotated.** It was pasted into a chat/editor buffer, so treat it as
   disclosed. Anyone holding it can forge grant *and* policy tokens for every guard trusting the
   matching public key — that is total compromise of the access-issuing chain.
2. **No press response carrying config may be logged verbatim** — that's §3 reads as well as
   `update_config`/`delete_config`, which echo the site's whole merged config back. When logging an FC failure, log the
   endpoint, the site, and the keys we submitted — never the response body. `fc_mis`'s habit of
   dumping `frappe.as_json(response)` into Error Log is unsafe for these two methods specifically.

Corollary worth noting: the hub itself runs on an FC site (`support-av.frappe.cloud` has
`access_hub` in `installed_apps`), so its private key is already in press's `Site Config`. That is
inherent to FC hosting, not something this integration introduces — but it does mean anyone with
team-level press access can read the hub's signing key. Worth a conscious decision rather than a
surprise.

---

## Summary — what the integration calls

| step | endpoint | doctype/method | cost |
|---|---|---|---|
| discover sites | `get_list` (or `site.all`) | `Site` → `name`, `host_name`, `limit: 0` | 1 |
| filter by app | `get_list` | `Site App` (parent = one site `name`) | **N**, no batching |
| check enrolled | `get_list` | `Site Config` (parent = one site `name`) | N (optional) |
| enroll | `run_doc_method` | `update_config` (merges, async) | 1 per site enrolled |
| un-enroll | `run_doc_method` | `delete_config` | 1 per key |

Everything is `POST … json=payload`. Discovery is `1 + N` and cannot be batched; measured at 35.8s
for 108 sites, which is fine in a `long` queue.

## Gotchas, collected

1. **GET fails** — use `post_api(..., json=...)`; `get_api` yields a bare `AttributeError`.
2. **Pagination is `limit`/`start`, default `limit: 20`.** Desk-style `limit_page_length`/`limit_start`
   are silently ignored, leaving exactly 20 rows and no error. There is no hard cap.
3. **Unknown fields/filters are dropped silently** — `validate_fields`/`validate_filters` keep only
   `dashboard_fields` + default/child-table fields. Requested keys can vanish and unrequested ones
   appear. Read defensively.
4. **`Site App` cannot be batched** — its controller `get_doc`s `filters["parent"]` as a single
   docname (§2). `Bench App` is not a workaround: bench apps ≠ site apps.
5. **`update_config` is async** — a 200 means *queued*, not applied (§4).
6. **Never log `update_config`/`delete_config` responses** — they echo the site's secrets (§6).
7. **`name` ≠ `host_name`** — address press by `name`, point browsers at `host_name` (§1).

## Reading the source

press is checked out at `/home/snv/Park/dev/apps/press` — read it rather than guessing at behaviour:

- `press/api/client.py` — `get_list` signature/defaults, `ALLOWED_DOCTYPES`, `validate_fields`,
  `validate_filters`, `is_allowed_field`, `apply_custom_filters`.
- `press/access/ownership.py` — `scope_query` / `scope_by_parent`, the generic team scoping.
- `press/press/doctype/<dt>/<dt>.py` — per-doctype `dashboard_fields` and `get_list_query` overrides.
  **Check for an override before assuming generic behaviour**; `Site`, `Site App`, `Bench App` and
  `Release Group App` all have one, and they're where the surprises live.
