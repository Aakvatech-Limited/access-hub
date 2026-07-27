"""Post-install / post-migrate setup that adapts to the site's other apps."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

HD_FORM_SCRIPT_NAME = "Access Hub - Ticket Sites Button"

# Helpdesk form script (frappe-ui). Shows an "Access Sites" button on a ticket
# only when the ticket has a customer AND the user can read Remote Site; clicking
# opens the desk Remote Site list filtered to that customer. Read-only, no token
# access — the desk list itself re-enforces Remote Site permissions.
_TICKET_SITES_BUTTON_SCRIPT = """
async function setupForm({ doc, call }) {
    // Gate up front: helpdesk renders every returned action unconditionally
    // (v-bind, no per-action condition), so hide the button by not emitting it.
    // The server owns both the permission check and the target URL — this
    // script hardcodes no desk path.
    if (!doc.customer) return { actions: [] };
    let url = null;
    try {
        url = await call("access_hub.api.ticket_sites_url", { customer: doc.customer });
    } catch (e) {
        url = null;
    }
    if (!url) return { actions: [] };

    return {
        actions: [
            {
                label: "Access Sites",
                onClick: () => window.open(url, "_blank"),
            },
        ],
    };
}
""".strip()


def after_install():
	"""Make the hub usable out of the box (idempotent): generate the signing
	keypair, seed default policy, then run the migrate-time app setup."""
	from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
		seed_default_policy,
	)
	from access_hub.utils.crypto import ensure_keypair

	ensure_keypair()
	seed_default_policy()
	after_migrate()


def after_migrate():
	"""Runs on every migrate (and from after_install). Idempotent."""
	if not _helpdesk_ready():
		return

	_sync_helpdesk_customer_field()
	_sync_helpdesk_ticket_form_script()


def _helpdesk_ready() -> bool:
	return "helpdesk" in frappe.get_installed_apps()


def _sync_helpdesk_customer_field():
	"""When Frappe Helpdesk is on the site, link a Remote Site to its HD
	Customer. System-generated custom field, optional — a hub-only convenience
	that ties a client site to its helpdesk customer record."""
	if frappe.db.exists("Custom Field", "Remote Site-customer"):
		return

	create_custom_field(
		"Remote Site",
		{
			"fieldname": "customer",
			"label": "Helpdesk Customer",
			"fieldtype": "Link",
			"options": "HD Customer",
			"insert_after": "column_break_status",
			"in_list_view": 1,
		},
	)


def _sync_helpdesk_ticket_form_script():
	"""Add/refresh the HD Form Script that renders the ticket's Access Sites
	button. Upserts so the script stays current across upgrades."""
	if not frappe.db.exists("DocType", "HD Form Script"):
		return

	values = {
		"dt": "HD Ticket",
		"apply_to": "Form",
		"enabled": 1,
		"script": _TICKET_SITES_BUTTON_SCRIPT,
	}
	if frappe.db.exists("HD Form Script", HD_FORM_SCRIPT_NAME):
		doc = frappe.get_doc("HD Form Script", HD_FORM_SCRIPT_NAME)
		doc.update(values)
	else:
		doc = frappe.get_doc({"doctype": "HD Form Script", "__newname": HD_FORM_SCRIPT_NAME, **values})

	doc.save(ignore_permissions=True)
