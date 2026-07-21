# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document

from access_hub.protocol import TRANSPORT_TOKEN_MIN_LEN, transport_token_hash


class RemoteSite(Document):
	def validate(self):
		self._generate_token_if_missing()
		self._validate_token_length()
		self._set_token_hash()
		self._validate_protocol_downgrade()

	def _generate_token_if_missing(self):
		"""If no plaintext token was provided (fresh doc, no hash yet either),
		autogenerate a CSPRNG token so the row is usable out of the box."""
		if not self.transport_token and not self.transport_token_hash:
			self.transport_token = secrets.token_urlsafe(32)

	def _validate_token_length(self):
		if self.transport_token and len(self.transport_token) < TRANSPORT_TOKEN_MIN_LEN:
			frappe.throw(
				f"Transport Token must be at least {TRANSPORT_TOKEN_MIN_LEN} characters long."
			)

	def _set_token_hash(self):
		"""Hash any newly-provided plaintext token. The plaintext is kept on the
		in-memory doc (so the save response can show it once to the operator)
		but is cleared from the database row right after commit."""
		if self.transport_token:
			self.transport_token_hash = transport_token_hash(self.transport_token)
			self._clear_plaintext_after_commit = True

	def _validate_protocol_downgrade(self):
		"""Never auto-downgrade https->http; only suggest http for :80 or
		*.localhost, and only as a default on a new doc, never forced."""
		# No auto-mutation here by design — protocol is user-editable and this
		# validate hook must not silently flip it. Kept as a no-op hook point
		# for future UI-side suggestion logic (client_script), per BUILD_PLAN.
		pass

	def on_update(self):
		if getattr(self, "_clear_plaintext_after_commit", False):
			frappe.db.set_value(self.doctype, self.name, "transport_token", "", update_modified=False)
			self._clear_plaintext_after_commit = False


def announce(host: str, port: int | None = None, protocol: str | None = None, app_version: str | None = None):
	"""Upsert a Remote Site row from a client's announce payload.

	Called by the ingest endpoint (already transport-token authenticated), so
	`host` here is the Remote Site matched by that authentication — this only
	updates metadata (port/protocol/app_version/last_announced_on), never the
	token/hash.
	"""
	if not frappe.db.exists("Remote Site", host):
		frappe.throw(f"Unknown Remote Site: {host}")

	doc = frappe.get_doc("Remote Site", host)
	if port:
		doc.port = port
	if protocol:
		doc.protocol = protocol
	if app_version:
		doc.app_version = app_version
	doc.last_announced_on = frappe.utils.now_datetime()
	doc.save(ignore_permissions=True)
	return doc
