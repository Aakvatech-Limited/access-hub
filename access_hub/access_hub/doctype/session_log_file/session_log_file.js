// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Session Log File", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Download Log"), () => {
			window.open(
				`/api/method/access_hub.api.download_session_log?name=${encodeURIComponent(frm.doc.name)}`,
				"_blank"
			);
		});

		frm.add_custom_button(__("View Lines"), () => {
			frappe.prompt(
				[
					{ fieldname: "start", fieldtype: "Int", label: __("Start Line"), default: 0 },
					{
						fieldname: "end",
						fieldtype: "Int",
						label: __("End Line (exclusive, blank = to end)"),
					},
				],
				(values) => {
					frappe.call({
						method: "access_hub.api.get_session_log_lines",
						args: {
							name: frm.doc.name,
							start: values.start || 0,
							end: values.end || null,
						},
						callback: (r) => {
							if (!r.message) {
								return;
							}
							const lines = r.message.lines || [];
							const html = `<pre style="max-height:60vh;overflow:auto;">${frappe.utils.escape_html(
								lines.join("\n")
							)}</pre>`;
							frappe.msgprint({
								title: __("Log lines for {0}", [frm.doc.jti]),
								message: html,
								wide: true,
							});
						},
					});
				},
				__("View Lines"),
				__("View")
			);
		});
	},
});
