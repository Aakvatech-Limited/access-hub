# Handover — parallel execution guide

Two Frappe apps implement one system. They can be built **in parallel** because the wire contract
between them is frozen up front. This doc is the entry point; hand each agent its app's
`docs/BUILD_PLAN.md`.

## The apps
- **access-hub** (`apps/access_hub`) — helpdesk/central: mints signed grants, holds the private key,
  registers client sites, ingests logs, serves policy. Not attacker-controlled → DocTypes fine.
- **access-guard** (`apps/access_guard`) — client-side: verifies grants, provisions/reaps ephemeral
  users, enforces deny/warn policy, logs to files. Attacker-controlled → integrity state in files.
- **access-protocol** — not a separate app. It is `docs/PROTOCOL.md` + the shared `protocol.py`,
  vendored **byte-identical** into both apps at `<app>/<app>/protocol.py`.

## What makes parallel safe
1. **`docs/PROTOCOL.md`** (identical in both apps) is the frozen contract — token/policy formats,
   `aud` binding, endpoints, file layout, log schema, errors, `pv`.
2. **`protocol.py`** (identical in both) is that contract as importable code.
3. Each app's tests are **self-contained** — the guard mints its own tokens with a throwaway
   keypair, so neither app's unit tests depend on the other running.

Rule: neither agent edits the other app. Any change to `PROTOCOL.md` / `protocol.py` is a
coordinated change applied to **both** copies (they must stay byte-identical).

## Suggested split
- Agent A → `apps/access_hub/docs/BUILD_PLAN.md`
- Agent B → `apps/access_guard/docs/BUILD_PLAN.md`

## One-time environment setup (either agent, once)
Bench root is `/home/snv/Park/15`. From there:
```
./env/bin/pip install pyseto           # both apps depend on it
bench new-site hub.localhost   --install-app access_hub   # if not present
bench new-site guard.localhost --install-app access_guard
bench --site hub.localhost   set-config developer_mode 1
bench --site guard.localhost set-config developer_mode 1
```
(Separate sites on purpose — hub and guard are two sides and must not share a DB.)

## Running tests
```
bench --site hub.localhost   run-tests --app access_hub
bench --site guard.localhost run-tests --app access_guard
```
Each app is "done" when its own suite is green (see its BUILD_PLAN "Done criteria").

## Integration (after both are green — a manual, human-run pass, not a unit test)
1. On hub: run the keypair command; copy the printed `k4.public` key.
2. Create a Remote Site row for `guard.localhost` (note its transport token).
3. Put `access_guard_hub_url`, `access_guard_hub_public_key`, `access_guard_transport_token` into
   guard's `site_config.json`.
4. Click Request access on the hub → confirm the browser lands as an ephemeral System Manager on
   the guard, a grant file exists, the monitoring bar shows, and a deny/warn op behaves per policy.
5. Confirm log push, announce, provisioning report, and policy pull round-trip over the transport
   channel. Full checklist: `docs/DESIGN.md` → "Verification (v1)".

## Notes
- `remote-access-plan/` (bench root) is a **temporary** planning folder; once these docs live in
  the apps it can be deleted — nothing there is authoritative anymore.
- Targets Frappe **v15/v16+**.
