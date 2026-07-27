// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Remote Site", {
	refresh(frm) {
		// Token freshly generated on the save just completed — show it once.
		const config = frm.doc.__onload && frm.doc.__onload.transport_token_config;
		if (config) {
			show_config_dialog(frm, config);
			delete frm.doc.__onload.transport_token_config;
		}

		if (frm.is_new()) {
			return;
		}

		// Any user who can see this site (Desk User / Agent get read-only) can
		// request access for themselves — mints a grant attributed to them.
		frm.add_custom_button(__("Request Access"))
			.addClass("btn-primary")
			.on("click", () => {
				frappe.call({
					method: "access_hub.api.request_access",
					args: { remote_site: frm.doc.name },
					freeze: true,
					freeze_message: __("Requesting access..."),
					callback: (r) => {
						if (r.message && r.message.redirect_url) {
							window.location.href = r.message.redirect_url;
						}
					},
				});
			});

		// Rotating the token writes to the doc, so gate on write permission —
		// read-only roles never see this, and rotate_transport_token's
		// doc.save() would reject them at the API even if they did.
		if (frm.perm[0] && frm.perm[0].write) {
			frm.add_custom_button(__("Update Token"), () => {
				frappe.prompt(
					[
						{
							fieldname: "token",
							fieldtype: "Data",
							label: __("New Token"),
							description: __("Leave blank to auto-generate a secure token."),
						},
					],
					(values) => {
						frappe.call({
							method: "access_hub.access_hub.doctype.remote_site.remote_site.rotate_transport_token",
							args: { remote_site: frm.doc.name, token: values.token || null },
							freeze: true,
							callback: (r) => {
								if (r.message) {
									frm.reload_doc();
									show_config_dialog(frm, r.message);
								}
							},
						});
					},
					__("Update Transport Token"),
					__("Update")
				);
			});
		}
	},
});

// The one dialog every generation/rotation path funnels into. A read-only Code
// field renders the config as copyable JSON with Frappe's built-in copy button.
function show_config_dialog(frm, config) {
	const blob = {
		access_guard_hub_url: config.access_guard_hub_url,
		access_guard_hub_public_key: config.access_guard_hub_public_key,
	};
	if (config.access_guard_transport_token) {
		blob.access_guard_transport_token = config.access_guard_transport_token;
	}

	const d = new frappe.ui.Dialog({
		title: __("Enrollment config for {0}", [frm.doc.host]),
		fields: [
			{
				fieldname: "config",
				fieldtype: "Code",
				label: __("access-guard site config"),
				options: "JSON",
				read_only: 1,
				default: JSON.stringify(blob, null, 2),
			},
			{
				fieldname: "note",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"The transport token is shown only now. Copy it before closing — only its hash is stored."
				)}</p>`,
			},
		],
	});
	d.show();
}
