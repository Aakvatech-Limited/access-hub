"""doc_events on User: disabling/deleting a User with grant history must
evict them everywhere immediately, not wait for the next Settings save.
Eviction itself is derived live (see AccessHubSettings.as_policy_dict) —
this module only decides when to rebuild + push."""

import frappe


def on_update(doc, method=None):
	if doc.has_value_changed("enabled") and not doc.enabled:
		_republish_policy()


def after_delete(doc, method=None):
	_republish_policy()


def _republish_policy():
	frappe.get_single("Access Hub Settings").republish_policy()
