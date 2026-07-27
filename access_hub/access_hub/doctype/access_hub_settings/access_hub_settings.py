# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from access_hub.protocol import (
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
)
from access_hub.utils.crypto import sign_paseto


class AccessHubSettings(Document):
	def validate(self):
		self._validate_rules()

	def _validate_rules(self):
		for row in self.rules:
			if not row.document_type:
				frappe.throw("Each rule row needs a Document Type.")

	def on_update(self):
		token = self.build_and_sign_policy_token()
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


def after_install():
	"""Idempotent seed: create the Access Hub Settings Single with the
	protocol's default deny rule if it doesn't already have any rules."""
	settings = frappe.get_single("Access Hub Settings")
	if settings.rules:
		return
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
