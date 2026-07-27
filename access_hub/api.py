"""Whitelisted API surface for access_hub, callable as `access_hub.api.<fn>`."""

import os
from functools import wraps
from urllib.parse import quote

import frappe

from access_hub.protocol import (
	TRANSPORT_TOKEN_HEADER,
	AccessProtocolError,
	TransportAuthError,
	check_pv,
	transport_token_hash,
)


@frappe.whitelist()
def request_access(remote_site: str):
	"""Mint a grant token for the current session user against `remote_site`,
	and return the guard accept URL the browser should be redirected to.
	PROTOCOL §3. Backs the "Request Access" button on the Remote Site form.

	Always mints for frappe.session.user — there is deliberately no
	`employee=` override. v1 has no approval step (DESIGN.md), and the whole
	point is attribution: an employee requests access for themselves, and it
	is logged as them. Letting any caller pass an arbitrary `employee` would
	let them mint a grant attributed to someone else's identity.

	Gated on the caller's own "read" permission for this Remote Site — the
	site is only requestable if their role can see it (Desk User / Agent get
	read-only). This never grants write, so it cannot rotate the token."""
	from access_hub.access_hub.doctype.access_grant.access_grant import create_and_mint

	site = frappe.get_doc("Remote Site", remote_site)
	site.check_permission("read")

	employee = frappe.session.user
	if employee in ("Administrator", "Guest"):
		frappe.throw("Please request access as a named user, not Administrator/Guest.")

	settings = frappe.get_single("Access Hub Settings")
	if settings.is_blacklisted(employee):
		frappe.throw("Access is currently disabled for this account.")

	_grant, token = create_and_mint(employee, site)
	return {"redirect_url": site.build_accept_url(token)}


@frappe.whitelist()
def ticket_sites_url(customer: str | None = None) -> str | None:
	"""URL of the Remote Site list filtered to `customer`, for the Helpdesk
	ticket "Access Sites" button. Returns None (button hides) unless there is a
	customer AND the user can read Remote Site — both gates enforced here, so
	the form script owns no policy and no desk path."""
	customer = (customer or "").strip()
	if not customer or not frappe.has_permission("Remote Site", "read"):
		return None
	return f"{frappe.utils.get_url_to_list('Remote Site')}?customer={quote(customer)}"


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
def push_logs(
	site,
	jti: str | None = None,
	log_kind: str = "request",
	lines: str = "",
):
	from access_hub.access_hub.doctype.session_log_file.session_log_file import (
		append_lines,
		upsert_index,
	)

	if not jti:
		frappe.throw("jti is required.")
	if log_kind not in ("request", "doc"):
		frappe.throw("log_kind must be 'request' or 'doc'.")

	added = append_lines(site.name, jti, log_kind, lines or "")
	upsert_index(site.name, jti, log_kind=log_kind, added_lines=added)

	return {"ok": True, "received_lines": added}


@ingest_endpoint
def report_session_event(site, jti: str | None = None, event: str | None = None, reason: str | None = None):
	"""Immediate (not hourly-batched) push of a session lifecycle event —
	accepted/login/logout — straight onto the matching Access Grant. Called
	right alongside access_guard.logging_.log_session_event, so the hub's
	view of "is this grant's session live" is near-real-time rather than
	waiting for the next hourly activity-log sync. Always on: there is no
	policy opt-out for this, unlike log_sync_enabled (which only gates the
	bulkier activity-log/announce/retention path)."""
	from access_hub.access_hub.doctype.access_grant.access_grant import record_session_event

	if not jti or not event:
		frappe.throw("jti and event are required.")
	record_session_event(jti, event, reason)
	return {"ok": True}


@frappe.whitelist()
def download_session_log(name: str):
	"""Serve the full session log file (by Session Log File `name`, never a
	client-supplied path) as a download. Gated on the Session Log File
	doctype's own "read" permission (System Manager). Line-range viewing is
	deferred to v2 — download-only for now."""
	from access_hub.access_hub.doctype.session_log_file.session_log_file import SUFFIX_BY_KIND, path_for

	doc = frappe.get_doc("Session Log File", name)
	doc.check_permission("read")
	path = path_for(doc.remote_site, doc.jti, doc.log_kind)

	if not os.path.exists(path):
		frappe.throw("Log file not found on disk.")

	with open(path, "rb") as f:
		content = f.read()

	suffix = SUFFIX_BY_KIND[doc.log_kind]
	frappe.local.response.filename = f"{doc.jti}{suffix}"
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
