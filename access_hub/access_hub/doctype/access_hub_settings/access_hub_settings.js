// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Access Hub Settings", {
	refresh(frm) {
		frappe.call({
			method: "access_hub.api.get_hub_enrollment_info",
			callback: (r) => r.message && render_enrollment_info(frm, r.message),
		});
	},
});

// Hub-wide enrollment values (same for every client). Rendered as copyable
// rows in the always-visible HTML field; the per-site transport token is set
// on each Remote Site, not here.
function render_enrollment_info(frm, info) {
	const rows = [
		["access_guard_hub_url", info.access_guard_hub_url],
		["access_guard_hub_public_key", info.access_guard_hub_public_key],
	];
	let html = "<table class='table table-bordered' style='table-layout:fixed;width:100%;'>";
	rows.forEach(([key, value], i) => {
		html += `
			<tr>
				<td style="width:35%;"><code>${key}</code></td>
				<td style="word-break:break-all;"><code>${frappe.utils.escape_html(value || "")}</code></td>
				<td style="width:1%;white-space:nowrap;">
					<button class="btn btn-xs btn-default" data-copy-row="${i}">${__("Copy")}</button>
				</td>
			</tr>`;
	});
	html += "</table>";

	const $wrapper = frm.get_field("hub_enrollment_info").$wrapper;
	$wrapper.html(html);
	// Copy the raw (unescaped) value from the closure, not the DOM.
	$wrapper.find("button[data-copy-row]").on("click", function () {
		frappe.utils.copy_to_clipboard(rows[$(this).attr("data-copy-row")][1]);
	});
}
