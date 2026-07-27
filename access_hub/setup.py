"""Post-install / post-migrate setup that adapts to the site's other apps."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def after_migrate():
	"""Runs on every migrate (and invoked from after_install). Idempotent."""
	_sync_helpdesk_customer_field()


def _sync_helpdesk_customer_field():
	"""When Frappe Helpdesk is on the site, link a Remote Site to its HD
	Customer. System-generated custom field, optional — a hub-only convenience
	that ties a client site to its helpdesk customer record."""
	if "helpdesk" not in frappe.get_installed_apps():
		return
	if not frappe.db.exists("DocType", "HD Customer"):
		return
	if frappe.db.exists("Custom Field", "Remote Site-customer"):
		return

	create_custom_field(
		"Remote Site",
		{
			"fieldname": "customer",
			"label": "Customer",
			"fieldtype": "Link",
			"options": "HD Customer",
			"insert_after": "host",
			"description": "Optional link to the Helpdesk customer for this client site.",
		},
	)
