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
		# Present only if nobody delivered the token for us - fc.push_guard_config
		# clears it once press has taken the config, so an FC site shows no dialog
		# while a site we can't reach still does.
		plaintext = self.flags.get("new_token_plaintext")
		if plaintext:
			self.set_onload("transport_token_config", self.get_enrollment_config(plaintext))

	def validate(self):
		# New doc with no token yet: autogenerate one so the row is usable out of
		# the box. `no_copy` on transport_token_hash means a copied doc arrives
		# here new with a blank hash, so this covers the copy case too. A token
		# deliberately pre-applied on a new doc (rotate-on-create) is kept.
		if self.is_new() and not self.transport_token_hash:
			self._apply_token(secrets.token_urlsafe(32))

		self.validate_fc_fields()

	def validate_fc_fields(self):
		"""The FC fields are all-or-nothing. Enforced here because the JSON can't:
		read_only and mandatory_depends_on are both UI-only, so any save() writes
		these freely. A half-set row would either hide fields behind the section's
		depends_on or have enrollment act on a site it can't address."""
		if not self.is_fc_site:
			if self.fc_site_id or self.fc_team or self.fc_enrolled_at:
				frappe.throw("Clear the Frappe Cloud fields together with Is FC Site, not separately.")
			return

		if not (self.fc_site_id or "").strip():
			frappe.throw(f"{self.meta.get_label('fc_site_id')} is required for a Frappe Cloud site.")

		# Derived, so the sync can't disagree with the settings - and every row
		# records its team for the day team becomes a table.
		self.fc_team = frappe.db.get_single_value("Access Hub Settings", "fc_team_name")

	def on_update(self):
		# access_disabled is an input to this site's policy token, so a flip has
		# to reach the guard for it to reap live sessions - push it now instead
		# of waiting up to an hour for that site's next pull. Narrowed to this
		# site: nothing here changes any other site's policy.
		if self.has_value_changed("access_disabled"):
			frappe.get_single("Access Hub Settings").republish_policy(site=self)

	def _apply_token(self, plaintext: str):
		"""Hash `plaintext` into transport_token_hash (plaintext never stored) and
		flag it as undelivered, so onload surfaces it for an operator to copy.
		Whoever delivers it instead (fc.push_guard_config) clears that flag."""
		if len(plaintext) < TRANSPORT_TOKEN_MIN_LEN:
			frappe.throw(f"Transport Token must be at least {TRANSPORT_TOKEN_MIN_LEN} characters long.")
		self.transport_token_hash = transport_token_hash(plaintext)
		self.flags.new_token_plaintext = plaintext

	def build_accept_url(self, token: str) -> str:
		base = f"{self.protocol}://{self.host}:{self.port}"
		return f"{base}/api/method/access_guard.api.accept?token={token}"

	def rotate_and_build_config(self) -> dict:
		"""Issue a fresh transport token and return the config carrying it. The one
		place both enrollment paths (operator copy-paste, FC push) get their three
		values from, so they can't drift."""
		plaintext_token = secrets.token_urlsafe(32)
		self._apply_token(plaintext_token)
		return self.get_enrollment_config(plaintext_token)

	def get_enrollment_config(self, plaintext_token: str | None = None) -> dict:
		"""The guard-side config an operator copies to enroll this site: hub URL,
		public key, and the one-time transport token (present only when freshly
		generated/rotated). Pure builder - rotate_and_build_config is what issues a
		token. Install generates the keypair, so a missing key is unexpected —
		surface it clearly rather than shipping half a config."""
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
	storing only its hash. doc.save() enforces write permission on the Remote Site.

	Returns `{"config": {...}}` for the copy dialog, or `{"pushed_to_fc": 1}` when
	this hub delivered the token to the site itself (fc.can_push_config).

	Either way the rotated doc rides back in the response (send_updated_docs), so
	the form shows the new hash without a reload."""
	from frappe.desk.form.save import send_updated_docs

	from access_hub import fc

	doc = frappe.get_doc("Remote Site", remote_site)
	plaintext = (token or "").strip() or secrets.token_urlsafe(32)
	doc._apply_token(plaintext)

	config = doc.get_enrollment_config(plaintext)
	pushed = fc.can_push_config(doc)
	if pushed:
		# Synchronously, and before the save: if press won't take it, the hub must
		# not be left expecting a token the site never got.
		fc.push_guard_config(doc, config)
		doc.fc_enrolled_at = frappe.utils.now_datetime()

	doc.save()
	send_updated_docs(doc)

	if pushed:
		return {"pushed_to_fc": 1, "host": doc.host}
	return {"config": config}
