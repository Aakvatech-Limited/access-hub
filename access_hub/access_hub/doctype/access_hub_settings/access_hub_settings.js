// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Access Hub Settings", {
	refresh(frm) {
		// Enrollment values are pushed via the controller's onload — no API call.
		const info = frm.doc.__onload && frm.doc.__onload.hub_enrollment_info;
		if (info) {
			render_enrollment_info(frm, info);
		}
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
