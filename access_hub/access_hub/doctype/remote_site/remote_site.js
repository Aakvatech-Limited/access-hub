// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Remote Site", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button(__("Get Enrollment Info"), () => {
			frappe.call({
				method: "access_hub.api.get_enrollment_info",
				args: { remote_site: frm.doc.name },
				callback: (r) => {
					if (!r.message) {
						return;
					}
					const msg = r.message;
					const rows = [
						["access_guard_hub_url", msg.access_guard_hub_url],
						["access_guard_hub_public_key", msg.access_guard_hub_public_key],
						[
							"access_guard_transport_token",
							msg.access_guard_transport_token || "(not available — see note below)",
						],
					];
					let html = "<table class='table table-bordered' style='width:100%;table-layout:fixed;'>";
					for (const [k, v] of rows) {
						html += `<tr><td style="width:40%;"><code>${k}</code></td><td style="word-break:break-all;"><code>${frappe.utils.escape_html(
							v || ""
						)}</code></td></tr>`;
					}
					html += "</table>";
					if (msg.transport_token_note) {
						html += `<p class="text-muted">${frappe.utils.escape_html(msg.transport_token_note)}</p>`;
					}
					const d = frappe.msgprint({
						title: __("Enrollment info for {0}", [frm.doc.name]),
						message: html,
						wide: true,
					});
					d.$wrapper.find(".modal-dialog").css("max-width", "700px");
				},
			});
		});
	},
});
