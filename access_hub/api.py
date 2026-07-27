"""Whitelisted API surface for access_hub, callable as `access_hub.api.<fn>`."""

import os
from functools import wraps

import frappe

from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL
from access_hub.protocol import (
	TRANSPORT_TOKEN_HEADER,
	AccessProtocolError,
	TransportAuthError,
	check_pv,
	transport_token_hash,
)


@frappe.whitelist()
def get_hub_enrollment_info():
	"""Return the hub-wide enrollment values shared by every client: the hub URL
	and public key. These are identical across all Remote Sites, so they live in
	Access Hub Settings rather than per-site. The per-site transport token is
	shown only at creation/rotation, via the Remote Site dialog.

	Gated on Access Hub Settings "read" (System Manager) since it returns the
	hub's public key.
	"""
	frappe.has_permission("Access Hub Settings", "read", throw=True)

	public_key = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
	if not public_key:
		frappe.throw("No hub keypair found. Run `bench access-hub-generate-keypair` first.")

	return {
		"access_guard_hub_url": frappe.utils.get_url(),
		"access_guard_hub_public_key": public_key,
	}


@frappe.whitelist()
def list_remote_sites():
	"""List Remote Sites for the Request Access UI.

	Deliberately NOT gated on the Remote Site doctype's own permissions
	(System Manager only, for its CRUD/admin surface) — per DESIGN.md, any
	employee clicks "Request access" for themselves; this is the read-only
	site-picker backing that flow, not the Remote Site admin list. `@frappe.
	whitelist()` without `allow_guest=True` already requires a real logged-in
	user (frappe.session.user not Guest). Response is intentionally minimal
	(no transport token / hash / any secret)."""
	return frappe.get_all(
		"Remote Site", fields=["name", "host", "port", "protocol", "is_fc_site"], order_by="host"
	)


@frappe.whitelist()
def request_access(remote_site: str):
	"""Mint a grant token for the current session user against `remote_site`,
	and return the guard accept URL the browser should be redirected to.
	PROTOCOL §3.

	Always mints for frappe.session.user — there is deliberately no
	`employee=` override. v1 has no approval step (DESIGN.md), and the whole
	point is attribution: an employee requests access for themselves, and it
	is logged as them. Letting any caller pass an arbitrary `employee` would
	let them mint a grant attributed to someone else's identity."""
	from access_hub.access_hub.doctype.access_grant.access_grant import (
		build_accept_url,
		create_and_mint,
	)

	employee = frappe.session.user
	if employee in ("Administrator", "Guest"):
		frappe.throw("Please request access as a named user, not Administrator/Guest.")

	_grant, token = create_and_mint(employee, remote_site)
	return {"redirect_url": build_accept_url(remote_site, token)}


# --- Ingest endpoints (PROTOCOL §5) -----------------------------------------
# Authenticated by transport token, sent raw (no "Bearer " prefix) in the
# custom TRANSPORT_TOKEN_HEADER — see protocol.py's comment above that
# constant for why this deliberately isn't the standard Authorization header.


def _get_transport_token() -> str | None:
	header = frappe.get_request_header(TRANSPORT_TOKEN_HEADER)
	if header:
		return header.strip() or None
	return None


def _authenticate_remote_site() -> "frappe.model.document.Document":
	token = _get_transport_token()
	if not token:
		raise TransportAuthError(f"Missing {TRANSPORT_TOKEN_HEADER} header.")

	token_hash = transport_token_hash(token)
	site_name = frappe.db.get_value("Remote Site", {"transport_token_hash": token_hash}, "name")
	if not site_name:
		raise TransportAuthError("Unrecognized transport token.")

	return frappe.get_doc("Remote Site", site_name)


def ingest_endpoint(fn):
	"""Drop-in `@frappe.whitelist(allow_guest=True)` for hub ingest endpoints
	(PROTOCOL §5). Authenticates the transport token by hash match, enforces
	`pv`, maps AccessProtocolError to the documented HTTP status.

	`wrapper` takes **kwargs and is @wraps(fn) (pattern: memberships/utils/
	webhooks.py in this bench) so frappe's dispatch filters+coerces kwargs
	against fn's real signature — real type coercion, not manual per-field
	parsing. `pv` is the exception: no `fn` declares it, so it's read from
	form_dict/kwargs directly rather than relying on that filtering.
	"""

	@wraps(fn)
	def wrapper(**kwargs):
		try:
			# pv isn't a real fn param, so real HTTP dispatch never puts it in
			# kwargs (filtered against fn's signature) — read form_dict too,
			# for direct-call tests that pass pv as an explicit kwarg.
			pv = kwargs.pop("pv", None) or frappe.local.form_dict.get("pv")
			if pv is not None:
				check_pv(int(pv) if isinstance(pv, str) else pv)

			site = _authenticate_remote_site()
			return fn(site, **kwargs)
		except AccessProtocolError as e:
			frappe.local.response.http_status_code = e.http_status
			return e.as_response()

	return frappe.whitelist(allow_guest=True)(wrapper)


@ingest_endpoint
def get_policy(site):
	from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
		get_current_policy_token,
	)

	return {"policy_token": get_current_policy_token()}


@ingest_endpoint
def push_logs(site, jti: str | None = None, host: str | None = None, session_meta=None, ndjson: str = ""):
	import json

	from access_hub.access_hub.doctype.session_log.session_log import get_or_create
	from access_hub.access_hub.doctype.session_log_file.session_log_file import (
		append_lines,
		upsert_index,
	)

	if not jti:
		frappe.throw("jti is required.")

	if isinstance(session_meta, str):
		try:
			session_meta = json.loads(session_meta) if session_meta else {}
		except ValueError:
			session_meta = {}
	session_meta = session_meta or {}

	get_or_create(jti, site.name, session_meta)

	added = append_lines(site.name, jti, ndjson or "")
	upsert_index(
		site.name,
		jti,
		employee=session_meta.get("employee") or session_meta.get("emp_email"),
		added_lines=added,
	)

	if session_meta.get("end_time") or session_meta.get("logout_reason"):
		log = frappe.get_doc("Session Log", jti)
		if session_meta.get("end_time"):
			log.end_time = session_meta["end_time"]
		if session_meta.get("logout_reason"):
			log.logout_reason = session_meta["logout_reason"]
		log.save(ignore_permissions=True)

	return {"ok": True, "received_lines": added}


@ingest_endpoint
def announce(
	site,
	host: str | None = None,
	port=None,
	protocol: str | None = None,
	app_version: str | None = None,
	**kwargs,
):
	from access_hub.access_hub.doctype.remote_site.remote_site import announce as announce_upsert

	announce_upsert(
		site.name,
		port=int(port) if port else None,
		protocol=protocol,
		app_version=app_version,
	)
	return {"ok": True}


@ingest_endpoint
def report_provisioning(
	site, jti: str | None = None, event: str | None = None, user: str | None = None, ts: str | None = None
):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Remote Site",
			"reference_name": site.name,
			"content": f"Provisioning report: jti={jti} event={event} user={user} ts={ts}",
		}
	).insert(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
def get_session_log_lines(name: str, start: int = 0, end: int | None = None):
	"""Serve a slice of a pushed session log by its Session Log File `name` —
	never a client-supplied path; path_for()'s traversal guard resolves the
	canonical on-disk path from remote_site+jti. Gated on the Session Log
	File doctype's own "read" permission (its permissions block currently
	scopes that to System Manager) rather than a hardcoded role check, so
	granting a future custom role read access there is enough — no code
	change needed here. Supports line-range partial views for very large
	files (DESIGN.md)."""
	frappe.has_permission("Session Log File", "read", doc=name, throw=True)

	from access_hub.access_hub.doctype.session_log_file.session_log_file import read_lines

	start = int(start) if start is not None else 0
	end = int(end) if end is not None else None
	return {"lines": read_lines(name, start=start, end=end)}


@frappe.whitelist()
def download_session_log(name: str):
	"""Serve the full session log file (by Session Log File `name`, never a
	client-supplied path) as a download. Gated on the Session Log File
	doctype's own "read" permission — see get_session_log_lines()."""
	frappe.has_permission("Session Log File", "read", doc=name, throw=True)

	from access_hub.access_hub.doctype.session_log_file.session_log_file import path_for

	doc = frappe.get_doc("Session Log File", name)
	path = path_for(doc.remote_site, doc.jti)

	if not os.path.exists(path):
		frappe.throw("Log file not found on disk.")

	with open(path, "rb") as f:
		content = f.read()

	frappe.local.response.filename = f"{doc.jti}.jsonl"
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
