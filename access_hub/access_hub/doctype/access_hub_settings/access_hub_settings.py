# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.desk.search import search_link
from frappe.model.document import Document

from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL, POLICY_TOKEN_TTL_DAYS
from access_hub.protocol import (
	GUARD_CONF_HUB_PUBLIC_KEY,
	GUARD_CONF_HUB_URL,
	POLICY_ACCESS_DISABLED,
	POLICY_ALWAYS_LOG_DOCTYPES,
	POLICY_BLACKLISTED_EMAILS,
	POLICY_DEFAULTS,
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
# create/write/submit/cancel/delete), so it'd be a checkbox that does nothing.
POLICY_ACTIONS = ("create", "write", "delete", "submit", "cancel")


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

	def validate(self):
		self._validate_rules()

	def _validate_rules(self):
		for row in self.rules:
			if not row.document_type:
				frappe.throw("Each rule row needs a Document Type.")

	def on_update(self):
		self.republish_policy()

	def republish_policy(self):
		"""Rebuild + push the signed policy token. Callable directly by
		anything that needs to force a republish outside of a Settings save
		(e.g. user_events on User disable/delete) without depending on
		on_update's other side effects."""
		# Before the keypair exists (e.g. right after install seeds defaults) we
		# can't sign yet. Leave the token empty; get_current_policy_token() builds
		# it lazily on first fetch, once `bench access-hub-generate-keypair` has run.
		try:
			token = self.build_and_sign_policy_token()
		except ClientNotConfiguredError:
			return
		frappe.db.set_value(self.doctype, self.name, "current_policy_token", token, update_modified=False)
		# Not configurable in v1 - always best-effort (backstopped by the
		# guard's unconditional hourly pull). Enqueued after commit so a
		# rolled-back save can never push a policy that was never saved.
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

		if not rules:
			rules = POLICY_DEFAULTS[POLICY_RULES]

		always_log = [
			row.document_type for row in self.always_log_doctypes if row.document_type
		] or POLICY_DEFAULTS[POLICY_ALWAYS_LOG_DOCTYPES]

		return {
			POLICY_RULES: rules,
			POLICY_SESSION_EXPIRY_SECONDS: self.session_expiry_seconds
			or POLICY_DEFAULTS[POLICY_SESSION_EXPIRY_SECONDS],
			POLICY_ALWAYS_LOG_DOCTYPES: always_log,
			POLICY_MAX_KEY_SIZE_BYTES: self.max_key_size_bytes or POLICY_DEFAULTS[POLICY_MAX_KEY_SIZE_BYTES],
			POLICY_LOG_READS: bool(self.log_reads),
			POLICY_LOG_SYNC_ENABLED: bool(self.log_sync_enabled),
			POLICY_RETENTION_DAYS: self.retention_days or POLICY_DEFAULTS[POLICY_RETENTION_DAYS],
			POLICY_PUSH_GRACE_DAYS: self.push_grace_days or POLICY_DEFAULTS[POLICY_PUSH_GRACE_DAYS],
			POLICY_NAME_SUFFIX: self.name_suffix or POLICY_DEFAULTS[POLICY_NAME_SUFFIX],
			POLICY_ACCESS_DISABLED: bool(self.access_disabled),
			# Unioned with disabled_granted_user_emails() (self-correcting, no cleanup needed).
			POLICY_BLACKLISTED_EMAILS: list(
				{row.user for row in self.blacklisted_users if row.user} | disabled_granted_user_emails()
			),
		}

	def is_blacklisted(self, employee: str) -> bool:
		if bool(self.access_disabled):
			return True
		if employee in {row.user for row in self.blacklisted_users if row.user}:
			return True
		return not bool(frappe.db.get_value("User", employee, "enabled"))

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


def disabled_granted_user_emails() -> set[str]:
	"""Emails ever granted access whose User is now disabled or gone.
	Derived live, not stored - re-enabling the User clears it automatically."""
	rows = frappe.db.sql(
		"""
		select distinct g.emp_email as emp_email
		from `tabAccess Grant` g
		left join `tabUser` u on u.name = g.employee and u.enabled = 1
		where g.emp_email is not null and u.name is null
		""",
		as_dict=True,
	)
	return {row.emp_email for row in rows}


@frappe.whitelist()
def doctype_autocomplete(txt: str | None = None) -> list:
	"""Autocomplete source for Always-Log Doctypes. A thin wrapper, not a
	direct client-side call to search_link: the Autocomplete control's
	frappe.call doesn't shape args (filters/page_length) the way the Link
	field's own search_link invocation does, and passing it raw 500s."""
	return search_link("DocType", txt or "")


def get_current_policy_token() -> str:
	"""Return the current signed policy token, (re)building it if missing."""
	settings = frappe.get_single("Access Hub Settings")
	token = frappe.db.get_single_value("Access Hub Settings", "current_policy_token")
	if token:
		return token
	token = settings.build_and_sign_policy_token()
	frappe.db.set_value(
		"Access Hub Settings", settings.name, "current_policy_token", token, update_modified=False
	)
	return token


def push_policy_to_all_sites(token: str):
	"""Best-effort push of the policy token to every registered Remote Site.
	Failures (offline/firewalled clients) are swallowed — the guard's guaranteed
	hourly pull covers those."""
	import requests

	for site in frappe.get_all("Remote Site", fields=["name", "host", "port", "protocol"]):
		url = f"{site.protocol}://{site.host}:{site.port}/api/method/access_guard.api.receive_policy"
		try:
			requests.post(url, json={"policy_token": token}, timeout=30)
		except Exception:
			frappe.logger("access_hub").info(f"policy push to {site.name} failed (best-effort, ignored)")


def seed_default_policy():
	"""Seed the Access Hub Settings Single with the protocol's default scalar
	values and deny rule, if it has no rules yet. Idempotent. Orchestrated from
	access_hub.setup on install."""
	settings = frappe.get_single("Access Hub Settings")
	if settings.rules:
		return

	# Seed the required scalar fields from protocol defaults. Field-level
	# defaults in the JSON cover the desk-created case, but a freshly-installed
	# Single loaded here has them blank — set them explicitly so save() passes.
	for fieldname, policy_key in (
		("session_expiry_seconds", POLICY_SESSION_EXPIRY_SECONDS),
		("max_key_size_bytes", POLICY_MAX_KEY_SIZE_BYTES),
		("retention_days", POLICY_RETENTION_DAYS),
		("push_grace_days", POLICY_PUSH_GRACE_DAYS),
		("name_suffix", POLICY_NAME_SUFFIX),
	):
		if not settings.get(fieldname):
			settings.set(fieldname, POLICY_DEFAULTS[policy_key])

	default_rule = POLICY_DEFAULTS[POLICY_RULES][0]
	row = {"document_type": default_rule["doctype"], "mode": default_rule["mode"]}
	# check the action fields named by the protocol default (no hardcoding)
	for action in default_rule["actions"]:
		row[f"action_{action}"] = 1
	settings.append("rules", row)

	for doctype in POLICY_DEFAULTS[POLICY_ALWAYS_LOG_DOCTYPES]:
		settings.append("always_log_doctypes", {"document_type": doctype})

	settings.save(ignore_permissions=True)
