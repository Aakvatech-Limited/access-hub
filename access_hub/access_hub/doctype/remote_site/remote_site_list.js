// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

// Per-row "Request Access" button in the Remote Site list — the quick path
// from the Helpdesk ticket's filtered list to actually opening a site. Mints a
// grant for the current user and redirects to the guard (same flow as the form
// button). request_access enforces read permission server-side.
frappe.listview_settings["Remote Site"] = {
	// Not in the list's default field set (only in_list_view fields are), and
	// both the indicator and the button read it.
	add_fields: ["access_disabled"],

	get_indicator(doc) {
		if (doc.access_disabled) {
			return [__("Disabled"), "grey", "access_disabled,=,1"];
		}
		return [__("Enabled"), "green", "access_disabled,=,0"];
	},

	button: {
		// Hidden for a disabled site — request_access refuses it server-side
		// regardless.
		show(doc) {
			return !doc.access_disabled;
		},
		get_label() {
			return __("Request Access");
		},
		get_description(doc) {
			return __("Request ephemeral access to {0}", [doc.host]);
		},
		action(doc) {
			frappe.call({
				method: "access_hub.api.request_access",
				args: { remote_site: doc.name },
				freeze: true,
				freeze_message: __("Requesting access..."),
				callback: (r) => {
					if (r.message && r.message.redirect_url) {
						window.open(r.message.redirect_url, "_blank");
					}
				},
			});
		},
	},
};
