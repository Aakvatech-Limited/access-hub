// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Access Hub Settings", {
	refresh(frm) {
		// Enrollment values are pushed via the controller's onload — no API call.
		const info = frm.doc.__onload && frm.doc.__onload.hub_enrollment_info;
		if (info) {
			render_enrollment_info(frm, info);
		}

		// Live DocType-name suggestions, but not enforced (ignore_validation on
		// the field): a doctype absent from THIS site (e.g. it only exists on a
		// client) is still a valid, harmless entry.
		const doctype_query = () => ({
			query: "access_hub.access_hub.doctype.access_hub_settings.access_hub_settings.doctype_autocomplete",
		});
		frm.set_query("document_type", "always_log_doctypes", doctype_query);
		frm.set_query("document_type", "rules", doctype_query);
	},

	// A Button field, not a toolbar action, so it sits beside the config it acts on
	// and hides with the section.
	fc_sync_now(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the settings first."));
			return;
		}
		frappe.call({
			method: "access_hub.fc.sync_now",
			freeze: true,
			freeze_message: __("Queueing Frappe Cloud sync..."),
			callback: () => {
				frappe.show_alert({
					message: __(
						"Frappe Cloud sync queued. Check Error Log if sites don't appear."
					),
					indicator: "green",
				});
			},
		});
	},

	access_disabled(frm) {
		if (!frm.doc.access_disabled) {
			return;
		}
		frappe.confirm(
			__(
				"On save, this will refuse new access requests and immediately end every active session on every client. Continue?"
			),
			() => {},
			() => frm.set_value("access_disabled", 0)
		);
	},
});

// Hub-wide enrollment values (same for every client). Rendered as copyable
// rows in the always-visible HTML field; the per-site transport token is set
// on each Remote Site, not here.
function render_enrollment_info(frm, info) {
	// Keys come from the onload payload (protocol-defined server-side) — the JS
	// hardcodes none of them.
	const rows = Object.entries(info);
	let html = "";
	rows.forEach(([key, value], i) => {
		html += `
			<div class="mb-3">
				<div class="text-muted small mb-1"><code>${key}</code></div>
				<div class="input-group">
					<input type="text" class="form-control" readonly
						style="border-top-right-radius:0;border-bottom-right-radius:0;"
						value="${frappe.utils.escape_html(value || "")}">
					<button class="btn btn-default" data-copy-row="${i}"
						style="border-top-left-radius:0;border-bottom-left-radius:0;margin-left:-1px;">${__(
							"Copy"
						)}</button>
				</div>
			</div>`;
	});

	const $wrapper = frm.get_field("hub_enrollment_info").$wrapper;
	$wrapper.html(html);
	// Copy the raw (unescaped) value from the closure, not the DOM.
	$wrapper.find("button[data-copy-row]").on("click", function () {
		frappe.utils.copy_to_clipboard(rows[$(this).attr("data-copy-row")][1]);
	});
}
