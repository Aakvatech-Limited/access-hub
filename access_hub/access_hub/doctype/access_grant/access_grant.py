# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from access_hub.constants import GRANT_TTL_SECONDS_DEFAULT
from access_hub.protocol import (
	CLAIM_JTI,
	CLAIM_PV,
	CLAIM_USER_EMAIL,
	CLAIM_USER_FIRST_NAME,
	CLAIM_USER_LAST_NAME,
	CLAIM_USER_MIDDLE_NAME,
	PV,
)
from access_hub.utils.crypto import sign_paseto


class AccessGrant(Document):
	pass


def create_and_mint(user: str, site: "frappe.model.document.Document") -> tuple["AccessGrant", str]:
	"""Create an Access Grant row for `user` (a User) against `site` (a
	Remote Site doc), mint its PASETO v4.public grant token per PROTOCOL §3,
	and return (grant_doc, token).
	"""
	if not site.transport_token_hash:
		frappe.throw(f"Remote Site {site.name!r} has no transport token hash set.")

	jti = uuid.uuid4().hex
	issued_at = now_datetime()
	expires_at = add_to_date(issued_at, seconds=GRANT_TTL_SECONDS_DEFAULT)

	grant = frappe.get_doc(
		{
			"doctype": "Access Grant",
			"user": user,
			"remote_site": site.name,
			"jti": jti,
			"status": "Issued",
			"issued_at": issued_at,
			"expires_at": expires_at,
		}
	)
	# user_name/user_email are fetch_from - insert() populates them from the
	# linked User, no separate lookup needed here.
	grant.insert(ignore_permissions=True)

	first_name, middle_name, last_name = frappe.db.get_value(
		"User", user, ["first_name", "middle_name", "last_name"]
	)

	claims = {
		CLAIM_USER_FIRST_NAME: first_name,
		CLAIM_USER_MIDDLE_NAME: middle_name,
		CLAIM_USER_LAST_NAME: last_name,
		CLAIM_USER_EMAIL: grant.user_email,
		"aud": site.transport_token_hash,
		CLAIM_JTI: jti,
		CLAIM_PV: PV,
		"iat": issued_at.isoformat(),
		"exp": expires_at.isoformat(),
	}
	token = sign_paseto(claims)
	return grant, token


def record_session_event(jti: str, event: str, reason: str | None = None) -> None:
	"""Applies a guard-reported session event to its Access Grant. Unknown
	jti is a no-op — the grant may already be reaped by the time this lands."""
	if not frappe.db.exists("Access Grant", jti):
		return
	grant = frappe.get_doc("Access Grant", jti)
	if event in ("accepted", "login"):
		grant.status = "Active"
		if not grant.start_time:
			grant.start_time = now_datetime()
	elif event == "logout":
		grant.status = "Ended"
		grant.end_time = now_datetime()
		grant.logout_reason = reason
	else:
		return
	grant.save(ignore_permissions=True)
