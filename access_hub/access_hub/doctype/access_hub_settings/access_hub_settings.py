# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.desk.search import search_link
from frappe.model.document import Document

from access_hub.constants import (
	CONF_HUB_PUBLIC_KEY_LOCAL,
	POLICY_TOKEN_CACHE_KEY,
	POLICY_TOKEN_CACHE_TTL_SECONDS,
	POLICY_TOKEN_TTL_DAYS,
)
from access_hub.protocol import (
	GUARD_CONF_HUB_PUBLIC_KEY,
	GUARD_CONF_HUB_URL,
	MODE_DENY,
	POLICY_ACCESS_DISABLED,
	POLICY_ALWAYS_LOG_DOCTYPES,
	POLICY_BLACKLISTED_EMAILS,
	POLICY_DISALLOW_SIMULTANEOUS_SESSIONS,
	POLICY_LOG_READS,
	POLICY_LOG_SYNC_ENABLED,
	POLICY_MAX_KEY_SIZE_BYTES,
	POLICY_NAME_SUFFIX,
	POLICY_PUSH_GRACE_DAYS,
	POLICY_RETENTION_DAYS,
	POLICY_RULES,
	POLICY_SESSION_EXPIRY_SECONDS,
	PV,
	ClientNotConfiguredError,
)
from access_hub.utils.crypto import sign_paseto

# Policy actions; each maps to an Access Policy Rule check field named
# `action_<action>` (the prefix avoids bare-verb fieldnames like `write`/`create`
# clashing with SQL/Frappe). Used both to read rows into the wire object and to
# seed defaults, so neither drifts from the protocol's action vocabulary.
# NOTE: no "read" — the guard never enforces it (doc_events only cover
# create/write/submit/cancel/delete/rename), so it'd be a checkbox that does
# nothing.
POLICY_ACTIONS = ("create", "write", "delete", "submit", "cancel", "rename")

# seed_default_policy()'s install-time child-table rows, already shaped for
# settings.append() — this is a one-time seed, not a wire-protocol concept, so
# it lives here rather than in the shared protocol.py.
DEFAULT_RULES = [
	{
		"document_type": "User",
		"mode": MODE_DENY,
		"action_create": 1,
		"action_write": 1,
		"action_delete": 1,
		"action_rename": 1,
	},
]
DEFAULT_ALWAYS_LOG_DOCTYPES = ["User", "Server Script", "Scheduled Job Type"]


class AccessHubSettings(Document):
	def onload(self):
		# Push the hub-wide enrollment values (same for every client) onto the
		# form via onload — no extra API round-trip. Keypair-tolerant: absent
		# before the first keygen, the field just shows nothing.
		public_key = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
		if public_key:
			self.set_onload(
				"hub_enrollment_info",
				{
					GUARD_CONF_HUB_URL: frappe.utils.get_url(),
					GUARD_CONF_HUB_PUBLIC_KEY: public_key,
				},
			)

	def on_update(self):
		self.republish_policy()

	def republish_policy(self):
		"""Rebuild + push the signed policy token. Callable directly by
		anything that needs to force a republish outside of a Settings save
		(e.g. user_events on User disable/delete) without depending on
		on_update's other side effects."""
		if self.is_new():
			return

		try:
			token = self.build_and_sign_policy_token()
		except ClientNotConfiguredError:
			return

		frappe.cache.set_value(POLICY_TOKEN_CACHE_KEY, token, expires_in_sec=POLICY_TOKEN_CACHE_TTL_SECONDS)
		frappe.enqueue(push_policy_to_all_sites, queue="long", enqueue_after_commit=True, token=token)

	def as_policy_dict(self) -> dict:
		"""Build the normalized policy object (protocol.POLICY_* keys) from this
		Single doctype's fields, per docs/PROTOCOL.md §4."""
		rules = []
		for row in self.rules:
			actions = [action for action in POLICY_ACTIONS if row.get(f"action_{action}")]
			if not actions:
				continue
			rules.append({"doctype": row.document_type, "actions": actions, "mode": row.mode})

		always_log = [row.document_type for row in self.always_log_doctypes if row.document_type]

		return {
			POLICY_RULES: rules,
			POLICY_SESSION_EXPIRY_SECONDS: self.session_expiry_seconds,
			POLICY_ALWAYS_LOG_DOCTYPES: always_log,
			POLICY_MAX_KEY_SIZE_BYTES: self.max_key_size_bytes,
			POLICY_LOG_READS: bool(self.log_reads),
			POLICY_LOG_SYNC_ENABLED: bool(self.log_sync_enabled),
			POLICY_RETENTION_DAYS: self.retention_days,
			POLICY_PUSH_GRACE_DAYS: self.push_grace_days,
			POLICY_NAME_SUFFIX: self.name_suffix or "",
			POLICY_ACCESS_DISABLED: bool(self.access_disabled),
			# Unioned with disabled_granted_user_emails() (self-correcting, no cleanup needed).
			POLICY_BLACKLISTED_EMAILS: list(blacklisted_user_emails(self) | disabled_granted_user_emails()),
			POLICY_DISALLOW_SIMULTANEOUS_SESSIONS: bool(self.disallow_simultaneous_sessions),
		}

	def is_blacklisted(self, user: str) -> bool:
		"""`user` is a User name (e.g. frappe.session.user) - blacklisted_users.user
		is also a User name (a Link), so this is a direct comparison, no email
		resolution needed (contrast as_policy_dict's POLICY_BLACKLISTED_EMAILS,
		which is the wire protocol's email-keyed shape for the guard)."""
		if bool(self.access_disabled):
			return True
		if user in {row.user for row in self.blacklisted_users if row.user}:
			return True
		return not bool(frappe.db.get_value("User", user, "enabled"))

	def build_and_sign_policy_token(self) -> str:
		policy = self.as_policy_dict()
		claims = dict(policy)
		claims["pv"] = PV
		# verify_and_decode() requires exp on every token it checks (grant or
		# policy) - one uniform check, no special-casing by token type. A
		# guard re-verifies on every save-triggered push and guaranteed hourly
		# pull, so this only bites an already-unreachable guard.
		now = frappe.utils.now_datetime()
		claims["iat"] = now.isoformat()
		claims["exp"] = frappe.utils.add_to_date(now, days=POLICY_TOKEN_TTL_DAYS).isoformat()
		return sign_paseto(claims)


def blacklisted_user_emails(settings: "AccessHubSettings") -> set[str]:
	"""Emails of admin-blacklisted Users. blacklisted_users.user is a Link to
	User (its name), not necessarily its email - resolve rather than assume."""
	names = {row.user for row in settings.blacklisted_users if row.user}
	if not names:
		return set()
	rows = frappe.get_all("User", filters={"name": ["in", list(names)]}, pluck="email")
	return {email for email in rows if email}


def disabled_granted_user_emails() -> set[str]:
	"""Emails ever granted access, minus emails of currently-enabled Users.
	All in email-space (g.user_email is a snapshot, not a Link) so this stays
	correct even if User.name stops being the email (e.g. a future UUID
	naming scheme). Derived live, not stored - re-enabling the User clears
	it automatically."""
	granted = frappe.get_all(
		"Access Grant", filters={"user_email": ["is", "set"]}, pluck="user_email", distinct=True
	)
	if not granted:
		return set()
	# email has no index, so filtering "email in granted" wouldn't use one
	# anyway - just fetch every enabled email once.
	enabled = frappe.get_all("User", filters={"enabled": 1}, pluck="email")
	return set(granted) - set(enabled)


@frappe.whitelist()
def doctype_autocomplete(txt: str | None = None) -> list:
	"""Autocomplete source for Always-Log Doctypes. A thin wrapper, not a
	direct client-side call to search_link: the Autocomplete control's
	frappe.call doesn't shape args (filters/page_length) the way the Link
	field's own search_link invocation does, and passing it raw 500s."""
	return search_link("DocType", txt or "")


def get_current_policy_token() -> str:
	"""Return a signed policy token, cached for POLICY_TOKEN_CACHE_TTL_SECONDS so
	the hourly pull from every guard doesn't re-sign on each request. A Settings
	save busts this immediately (see republish_policy) - the cache TTL only
	bounds the delay for anything that skips that path (e.g. install seeding).

	expires=True: get_value's default would cache a miss into
	frappe.local.cache, but set_value (called with expires_in_sec, as
	republish_policy always does) skips that cache on writes - so a miss
	here followed by a write there would leave frappe.local.cache stuck on a
	stale None. expires=True avoids frappe.local.cache entirely."""
	token = frappe.cache.get_value(POLICY_TOKEN_CACHE_KEY, expires=True)
	if token:
		return token
	token = frappe.get_single("Access Hub Settings").build_and_sign_policy_token()
	frappe.cache.set_value(POLICY_TOKEN_CACHE_KEY, token, expires_in_sec=POLICY_TOKEN_CACHE_TTL_SECONDS)
	return token


def push_policy_to_all_sites(token: str):
	"""Best-effort push of the policy token to every registered Remote Site.
	Failures (offline/firewalled clients) are swallowed — the guard's guaranteed
	hourly pull covers those."""
	import requests

	for site in frappe.get_all("Remote Site", fields=["name", "host", "port", "protocol"]):
		url = f"{site.protocol}://{site.host}:{site.port}/api/method/access_guard.api.receive_policy"
		try:
			requests.post(url, json={"token": token}, timeout=30)
		except Exception:
			frappe.log_error(title=f"access_hub: policy push to {site.name} failed")


def seed_default_policy():
	"""Seed the Access Hub Settings Single with DEFAULT_RULES/
	DEFAULT_ALWAYS_LOG_DOCTYPES, if it has no rules yet. Idempotent.
	Orchestrated from access_hub.setup on install.

	Built via new_doc, not get_single: a Single loaded with get_single() from
	an existing (even if all-blank) row never re-applies field defaults
	(is_new() is False, so Document._set_defaults() never runs) - new_doc()
	always does, so the scalar fields (session_expiry_seconds, name_suffix,
	etc) come straight from their JSON `default`, with nothing to duplicate
	here."""
	if frappe.db.exists("Access Policy Rule", {"parent": "Access Hub Settings"}):
		return
	settings = frappe.new_doc("Access Hub Settings")

	for row in DEFAULT_RULES:
		settings.append("rules", row)
	for doctype in DEFAULT_ALWAYS_LOG_DOCTYPES:
		settings.append("always_log_doctypes", {"document_type": doctype})

	settings.save(ignore_permissions=True)
