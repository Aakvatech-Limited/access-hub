// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Session Log File", {
	refresh(frm) {
		frm.disable_form();

		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Download Log"), () => {
			window.open(
				`/api/method/access_hub.api.download_session_log?name=${encodeURIComponent(
					frm.doc.name
				)}`,
				"_blank"
			);
		});
	},
});
