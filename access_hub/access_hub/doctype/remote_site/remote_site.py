# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document

from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL, TRANSPORT_TOKEN_MIN_LEN
from access_hub.protocol import (
	GUARD_CONF_HUB_PUBLIC_KEY,
	GUARD_CONF_HUB_URL,
	GUARD_CONF_TRANSPORT_TOKEN,
	transport_token_hash,
)


class RemoteSite(Document):
	def onload(self):
		# savedocs() runs onload on the same doc right after save and serializes
		# __onload into the save response — so a token applied during this write
		# (create autogen or rotation) reaches the form's frm.doc.__onload with no
		# reload. The plaintext lives only in a request-scoped flag; never stored.
		plaintext = self.flags.get("new_token_plaintext")
		if plaintext:
			self.set_onload("transport_token_config", self.get_enrollment_config(plaintext))

	def validate(self):
		# Fresh doc with no token yet: autogenerate one so the row is usable out
		# of the box. Only the hash is stored.
		if self.is_new() and not self.transport_token_hash:
			self._apply_token(secrets.token_urlsafe(32))

	def _apply_token(self, plaintext: str):
		"""Hash `plaintext` into transport_token_hash (plaintext never stored) and
		flag it for onload to surface once. Every token-setting path (create
		autogen, manual rotation) goes through here, so the copy dialog shows on
		all of them."""
		if len(plaintext) < TRANSPORT_TOKEN_MIN_LEN:
			frappe.throw(f"Transport Token must be at least {TRANSPORT_TOKEN_MIN_LEN} characters long.")
		self.transport_token_hash = transport_token_hash(plaintext)
		self.flags.new_token_plaintext = plaintext

	def get_enrollment_config(self, plaintext_token: str | None = None) -> dict:
		"""The guard-side config an operator copies to enroll this site: hub URL,
		public key, and the one-time transport token (present only when freshly
		generated/rotated). Install generates the keypair, so a missing key is
		unexpected — surface it clearly rather than shipping half a config."""
		public_key = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
		if not public_key:
			frappe.throw("No hub keypair found. Run `bench access-hub-generate-keypair` first.")
		return {
			GUARD_CONF_HUB_URL: frappe.utils.get_url(),
			GUARD_CONF_HUB_PUBLIC_KEY: public_key,
			GUARD_CONF_TRANSPORT_TOKEN: plaintext_token,
		}


@frappe.whitelist()
def rotate_transport_token(remote_site: str, token: str | None = None) -> dict:
	"""Set (or autogenerate, when `token` is blank) this site's transport token,
	storing only its hash, and return the one-time enrollment config for the copy
	dialog. doc.save() enforces write permission on the Remote Site."""
	doc = frappe.get_doc("Remote Site", remote_site)
	plaintext = (token or "").strip() or secrets.token_urlsafe(32)
	doc._apply_token(plaintext)
	doc.save()
	return doc.get_enrollment_config(plaintext)


def announce(
	remote_site: str, port: int | None = None, protocol: str | None = None, app_version: str | None = None
):
	"""Update a Remote Site's announce metadata.

	Called by the ingest endpoint with the Remote Site name matched by transport
	-token auth (not the payload host — host is now just a mutable title field,
	no longer the doc name). Updates port/protocol/app_version/last_announced_on
	only, never the token/hash.
	"""
	if not frappe.db.exists("Remote Site", remote_site):
		frappe.throw(f"Unknown Remote Site: {remote_site}")

	doc = frappe.get_doc("Remote Site", remote_site)
	if port:
		doc.port = port
	if protocol:
		doc.protocol = protocol
	if app_version:
		doc.app_version = app_version
	doc.last_announced_on = frappe.utils.now_datetime()
	doc.save(ignore_permissions=True)
	return doc
