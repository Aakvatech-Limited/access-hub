"""doc_events on User: enabling/disabling/deleting a User with grant history
must evict or un-evict them everywhere immediately, not wait for the next
Settings save (also covers a new User reusing a previously-evicted email).
Eviction itself is derived live (see AccessHubSettings.as_policy_dict) —
this module only decides when to rebuild + push."""

import frappe


def on_update(doc, method=None):
	if doc.has_value_changed("enabled"):
		_republish_policy()


def on_trash(doc, method=None):
	_republish_policy()


def _republish_policy():
	frappe.get_single("Access Hub Settings").republish_policy()
