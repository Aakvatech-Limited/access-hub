# Operations guide

How to install, enroll clients, use, and run the two apps. For *why* it's built
this way see [DESIGN.md](DESIGN.md); for the wire contract, [PROTOCOL.md](PROTOCOL.md).

- **access-hub** runs on the **helpdesk** site.
- **access-guard** runs on **each client** site you want to grant access to.

---

## 1. Install the hub

```bash
bench get-app https://github.com/Aakvatech-Limited/access-hub --branch develop
bench --site <helpdesk-site> install-app access_hub
```

Install auto-generates the PASETO signing keypair into the site's
`site_config.json` (`access_hub_private_key` + `access_hub_public_key`) and seeds
the default policy (`User → deny create/write/delete`). Nothing else to configure
to get started.

To regenerate the keypair (rotation):

```bash
bench --site <helpdesk-site> access-hub-generate-keypair   # --force to overwrite
```

The hub URL + public key are shown on the **Access Hub Settings** form (see step 3).

## 2. Install a guard (on a client site)

```bash
bench get-app https://github.com/Aakvatech-Limited/access-guard --branch develop
bench --site <client-site> install-app access_guard
```

The guard needs three config values in the client site's `site_config.json`
(see step 3 for where they come from):

| key | value |
|---|---|
| `access_guard_hub_url` | the helpdesk's base URL |
| `access_guard_hub_public_key` | the hub's `k4.public...` key |
| `access_guard_transport_token` | the per-client token (min length 8) |

If any is missing the guard refuses to accept grants (`ClientNotConfiguredError`).

## 3. Enroll a client (hub side)

1. Create a **Remote Site** record for the client (set `host`, `port`, `protocol`;
   tick **Is FC Site** if it's Frappe-Cloud-managed). On save, a transport token is
   auto-generated and a dialog shows the full enrollment config **once** (all three
   `access_guard_*` values) — copy it now; only the token's hash is stored.
   - To set a specific token or rotate later: **Update Token** button (leave blank
     to auto-generate). The dialog reappears with the new config.
2. Put the three values into the client site's `site_config.json`.

The token is shown only at creation/rotation — if you lose it, rotate to get a new
one (this invalidates the old one).

## 4. Request access (as an employee)

Two entry points, both mint a grant **for the logged-in user** and redirect the
browser to the client site as an ephemeral System Manager:

- **Remote Site form** → **Request Access** button.
- **Remote Site list** → per-row **Request Access** button (handy when filtered by
  customer).

**With Helpdesk installed:** an **Access Sites** button appears on an HD Ticket
when the ticket has a Customer and you can read Remote Site — it opens the Remote
Site list filtered to that customer, where each row's Request Access gets you in.

Who can request: **System Manager**, **Desk User**, and **Agent** can read Remote
Site and request access. Only System Manager (or write access) can create/edit a
Remote Site or rotate a token.

The granted session is idle-expiry bound (default 6h, or the client's own lower
limit); it's reaped automatically ~15 min after going idle, and on logout.

## 5. View audit logs (hub side)

Client sites push per-session logs to the hub hourly. On the hub:

- **Session Log** — one row per session (client, employee, jti, start/end, logout
  reason).
- **Session Log File** — the pushed `.jsonl` per session; open a record to view or
  download the line-by-line activity log. Served safely (path-traversal-guarded),
  System Manager only.

## 6. Development / testing

Hub and guard are two sides and must run on **separate sites** (never share a DB).

```bash
./env/bin/pip install pyseto            # both apps depend on it
bench new-site hub.localhost   --install-app access_hub
bench new-site guard.localhost --install-app access_guard
bench --site hub.localhost   set-config developer_mode 1
bench --site guard.localhost set-config developer_mode 1
```

Run the suites (each app's tests are self-contained — the guard mints its own
tokens with a throwaway keypair, so neither depends on the other running):

```bash
bench --site hub.localhost   run-tests --app access_hub
bench --site guard.localhost run-tests --app access_guard
```

**End-to-end smoke test** (after both are installed and enrolled per steps 1–3):
click **Request Access** on the hub → confirm the browser lands as an ephemeral
System Manager on the guard, a grant file exists under
`site/private/access_guard/grants/`, the monitoring notice shows, and a
policy-denied op (e.g. creating a User) is blocked. Then confirm log push,
announce, provisioning report, and policy pull round-trip over the transport
channel.
