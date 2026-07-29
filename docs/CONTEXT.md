# Project context

The problem this system solves, in the words of the consultancy that needs it.
For how it's built, see [DESIGN.md](DESIGN.md); for the wire contract,
[PROTOCOL.md](PROTOCOL.md); to run/operate it, [OPERATIONS.md](OPERATIONS.md).
Frappe Cloud enrollment automation: [FC_INTEGRATION.md](FC_INTEGRATION.md) (design)
and [FC_API.md](FC_API.md) (the press endpoints it calls).

## Problem statement

We run an ERPNext implementation / consultancy company with hundreds of end-user
clients and tens of employees who implement and support those clients.

Today each employee has a user account **on every client site**, and it must be
**System Manager** because much of the work needs elevated permissions.

Despite signed NDAs, a few employees have misused those permissions — causing data
breaches and lost client trust. Standing elevated access is the root cause.

We also run a central **helpdesk site** where clients and employees interact over
tickets.

## Proposed solution

**1. Ephemeral access.** An employee requests access from the helpdesk and gets a
short-lived account on the client site; when the work is done the account is
deleted automatically. Access exists only during the window it's needed.

**2. Logging.** Every action an employee performs on a client site is logged for
audit — who accessed which site, what they did, when. Logs **cannot** live in the
client-site DB (an employee could delete them), so they are written to **files**
on the client site and synced periodically to the helpdesk for monitoring.

## Hard constraint

No per-client base user with a copied API key-pair. A long-lived api_key/api_secret
stored per client on the helpdesk would itself be the standing, copyable,
breach-amplifying credential we're trying to remove. The design provisions access
**without** any shared secret per client — one helpdesk private key, clients hold
only the matching public key.

Two Frappe apps implement this: **access-hub** on the helpdesk, **access-guard** on
each client site.
