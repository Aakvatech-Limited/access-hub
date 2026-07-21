# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SessionLog(Document):
	pass


def get_or_create(jti: str, remote_site: str, session_meta: dict | None = None) -> "SessionLog":
	if frappe.db.exists("Session Log", jti):
		return frappe.get_doc("Session Log", jti)

	session_meta = session_meta or {}
	doc = frappe.get_doc(
		{
			"doctype": "Session Log",
			"jti": jti,
			"remote_site": remote_site,
			"employee": session_meta.get("employee") or session_meta.get("emp_email"),
			"employee_name": session_meta.get("employee_name") or session_meta.get("emp_name"),
			"start_time": session_meta.get("start_time") or frappe.utils.now_datetime(),
			"end_time": session_meta.get("end_time"),
			"logout_reason": session_meta.get("logout_reason"),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc
