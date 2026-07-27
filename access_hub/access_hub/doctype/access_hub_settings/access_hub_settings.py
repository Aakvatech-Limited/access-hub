# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL
from access_hub.protocol import (
	GUARD_CONF_HUB_PUBLIC_KEY,
	GUARD_CONF_HUB_URL,
	POLICY_ALWAYS_LOG_DOCTYPES,
	POLICY_DEFAULTS,
	POLICY_LOG_READS,
	POLICY_MAX_KEY_SIZE_BYTES,
	POLICY_NAME_SUFFIX,
	POLICY_PUSH_GRACE_DAYS,
	POLICY_RETENTION_DAYS,
	POLICY_RULES,
	POLICY_SESSION_EXPIRY_SECONDS,
	POLICY_SYNC_ENABLED,
	PV,
	ClientNotConfiguredError,
)
from access_hub.utils.crypto import sign_paseto


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
		# Before the keypair exists (e.g. right after install seeds defaults) we
		# can't sign yet. Leave the token empty; get_current_policy_token() builds
		# it lazily on first fetch, once `bench access-hub-generate-keypair` has run.
		try:
			token = self.build_and_sign_policy_token()
		except ClientNotConfiguredError:
			return
		frappe.db.set_value(self.doctype, self.name, "current_policy_token", token, update_modified=False)
		if self.sync_enabled:
			push_policy_to_all_sites(token)

	def as_policy_dict(self) -> dict:
		"""Build the normalized policy object (protocol.POLICY_* keys) from this
		Single doctype's fields, per docs/PROTOCOL.md §4."""
		rules = []
		for row in self.rules:
			actions = []
			for action, fieldname in (
				("create", "action_create"),
				("write", "action_write"),
				("delete", "action_delete"),
				("submit", "action_submit"),
				("cancel", "action_cancel"),
				("read", "action_read"),
			):
				if row.get(fieldname):
					actions.append(action)
			if not actions:
				continue
			rules.append({"doctype": row.document_type, "actions": actions, "mode": row.mode})

		if not rules:
			rules = POLICY_DEFAULTS[POLICY_RULES]

		always_log = [
			d.strip() for d in (self.always_log_doctypes or "").splitlines() if d.strip()
		] or POLICY_DEFAULTS[POLICY_ALWAYS_LOG_DOCTYPES]

		return {
			POLICY_RULES: rules,
			POLICY_SESSION_EXPIRY_SECONDS: self.session_expiry_seconds
			or POLICY_DEFAULTS[POLICY_SESSION_EXPIRY_SECONDS],
			POLICY_ALWAYS_LOG_DOCTYPES: always_log,
			POLICY_MAX_KEY_SIZE_BYTES: self.max_key_size_bytes or POLICY_DEFAULTS[POLICY_MAX_KEY_SIZE_BYTES],
			POLICY_LOG_READS: bool(self.log_reads),
			POLICY_SYNC_ENABLED: bool(self.sync_enabled),
			POLICY_RETENTION_DAYS: self.retention_days or POLICY_DEFAULTS[POLICY_RETENTION_DAYS],
			POLICY_PUSH_GRACE_DAYS: self.push_grace_days or POLICY_DEFAULTS[POLICY_PUSH_GRACE_DAYS],
			POLICY_NAME_SUFFIX: self.name_suffix or POLICY_DEFAULTS[POLICY_NAME_SUFFIX],
		}

	def build_and_sign_policy_token(self) -> str:
		policy = self.as_policy_dict()
		claims = dict(policy)
		claims["pv"] = PV
		return sign_paseto(claims)


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
			requests.post(url, json={"policy_token": token}, timeout=5)
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
	settings.append(
		"rules",
		{
			"document_type": default_rule["doctype"],
			"mode": default_rule["mode"],
			"action_create": 1,
			"action_write": 1,
			"action_delete": 1,
		},
	)
	settings.save(ignore_permissions=True)
