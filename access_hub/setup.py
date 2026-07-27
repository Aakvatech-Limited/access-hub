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
    if (!doc.customer) return { actions: [] };
    let canRead = false;
    try {
        canRead = await call("access_hub.api.can_read_remote_site");
    } catch (e) {
        canRead = false;
    }
    if (!canRead) return { actions: [] };

    return {
        actions: [
            {
                label: "Access Sites",
                icon: "server",
                onClick: () => {
                    const url = "/app/remote-site?customer=" + encodeURIComponent(doc.customer);
                    window.open(url, "_blank");
                },
            },
        ],
    };
}
""".strip()


def after_migrate():
	"""Runs on every migrate (and invoked from after_install). Idempotent."""
	_sync_helpdesk_customer_field()
	_sync_helpdesk_ticket_form_script()


def _helpdesk_ready() -> bool:
	return "helpdesk" in frappe.get_installed_apps() and frappe.db.exists("DocType", "HD Customer")


def _sync_helpdesk_customer_field():
	"""When Frappe Helpdesk is on the site, link a Remote Site to its HD
	Customer. System-generated custom field, optional — a hub-only convenience
	that ties a client site to its helpdesk customer record."""
	if not _helpdesk_ready():
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


def _sync_helpdesk_ticket_form_script():
	"""Add/refresh the HD Form Script that renders the ticket's Access Sites
	button. Upserts so the script stays current across upgrades."""
	if not _helpdesk_ready() or not frappe.db.exists("DocType", "HD Form Script"):
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
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "HD Form Script", "__newname": HD_FORM_SCRIPT_NAME, **values})
		doc.insert(ignore_permissions=True)
