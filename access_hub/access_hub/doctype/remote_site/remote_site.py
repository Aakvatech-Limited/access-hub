# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document

from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL, TRANSPORT_TOKEN_MIN_LEN
from access_hub.protocol import transport_token_hash


class RemoteSite(Document):
	def validate(self):
		# Fresh doc with no token yet: autogenerate one so the row is usable out
		# of the box. The plaintext is surfaced once via set_onload (read by the
		# form on the save response), never stored — only its hash lives on.
		if self.is_new() and not self.transport_token_hash:
			self._apply_token(secrets.token_urlsafe(32), onload=True)

	def _apply_token(self, plaintext: str, onload: bool = False):
		"""Hash `plaintext` into transport_token_hash. If `onload`, stash the
		enrollment config (incl. the one-time plaintext) on the onload payload so
		the form can show the copy dialog right after save."""
		if len(plaintext) < TRANSPORT_TOKEN_MIN_LEN:
			frappe.throw(f"Transport Token must be at least {TRANSPORT_TOKEN_MIN_LEN} characters long.")
		self.transport_token_hash = transport_token_hash(plaintext)
		if onload:
			self.set_onload("transport_token_config", self.enrollment_config(plaintext))

	def enrollment_config(self, plaintext_token: str | None = None) -> dict:
		"""The guard-side config an operator copies to enroll this site: hub URL,
		public key, and the one-time transport token (present only when freshly
		generated/rotated). Install generates the keypair, so a missing key is
		unexpected — surface it clearly rather than shipping half a config."""
		public_key = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
		if not public_key:
			frappe.throw("No hub keypair found. Run `bench access-hub-generate-keypair` first.")
		return {
			"access_guard_hub_url": frappe.utils.get_url(),
			"access_guard_hub_public_key": public_key,
			"access_guard_transport_token": plaintext_token,
		}


@frappe.whitelist()
def rotate_transport_token(remote_site: str, token: str | None = None) -> dict:
	"""Set (or autogenerate, when `token` is blank) this site's transport token,
	storing only its hash. Returns the enrollment config with the one-time
	plaintext so the form can show the copy dialog. doc.save() enforces write
	permission on the Remote Site."""
	doc = frappe.get_doc("Remote Site", remote_site)
	plaintext = (token or "").strip() or secrets.token_urlsafe(32)
	doc._apply_token(plaintext)
	doc.save()
	return doc.enrollment_config(plaintext)


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
