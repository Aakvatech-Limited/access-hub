# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from access_hub.constants import GRANT_TTL_SECONDS_DEFAULT
from access_hub.protocol import (
	CLAIM_EMP_EMAIL,
	CLAIM_EMP_NAME,
	CLAIM_JTI,
	CLAIM_PV,
	PV,
)
from access_hub.utils.crypto import sign_paseto


class AccessGrant(Document):
	pass


def create_and_mint(employee: str, remote_site: str) -> tuple["AccessGrant", str]:
	"""Create an Access Grant row for `employee` (a User) against `remote_site`
	(a Remote Site), mint its PASETO v4.public grant token per PROTOCOL §3, and
	return (grant_doc, token).
	"""
	site = frappe.get_doc("Remote Site", remote_site)
	if not site.transport_token_hash:
		frappe.throw(f"Remote Site {remote_site!r} has no transport token hash set.")

	user = frappe.get_doc("User", employee)

	jti = uuid.uuid4().hex
	issued_at = now_datetime()
	expires_at = add_to_date(issued_at, seconds=GRANT_TTL_SECONDS_DEFAULT)

	grant = frappe.get_doc(
		{
			"doctype": "Access Grant",
			"employee": employee,
			"remote_site": remote_site,
			"jti": jti,
			"status": "Issued",
			"issued_at": issued_at,
			"expires_at": expires_at,
		}
	)
	grant.insert(ignore_permissions=True)

	claims = {
		CLAIM_EMP_NAME: user.full_name,
		CLAIM_EMP_EMAIL: user.email or user.name,
		"aud": site.transport_token_hash,
		CLAIM_JTI: jti,
		CLAIM_PV: PV,
		"iat": issued_at.isoformat(),
		"exp": expires_at.isoformat(),
	}
	token = sign_paseto(claims)
	return grant, token


def build_accept_url(remote_site: str, token: str) -> str:
	site = frappe.get_doc("Remote Site", remote_site)
	base = f"{site.protocol}://{site.host}:{site.port}"
	return f"{base}/api/method/access_guard.api.accept?token={token}"
