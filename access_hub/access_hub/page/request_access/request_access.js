frappe.pages["request-access"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Request Access",
		single_column: true,
	});

	const $body = $("<div class='request-access-page' style='max-width: 640px; margin: 20px auto;'></div>").appendTo(
		page.body
	);

	const $select = $(
		"<div class='form-group'><label>Remote Site</label><select class='form-control'></select></div>"
	).appendTo($body);
	const $select_el = $select.find("select");

	const $button = $("<button class='btn btn-primary' style='margin-top: 10px;'>Request Access</button>").appendTo(
		$body
	);

	frappe.call({ method: "access_hub.api.list_remote_sites" }).then((r) => {
		(r.message || []).forEach((site) => {
			$select_el.append(
				`<option value="${frappe.utils.escape_html(site.name)}">${frappe.utils.escape_html(
					site.host
				)}</option>`
			);
		});
	});

	$button.on("click", () => {
		const remote_site = $select_el.val();
		if (!remote_site) {
			frappe.msgprint(__("Select a Remote Site first."));
			return;
		}
		frappe.call({
			method: "access_hub.api.request_access",
			args: { remote_site },
			freeze: true,
			freeze_message: __("Requesting access..."),
			callback: (r) => {
				if (r.message && r.message.redirect_url) {
					window.location.href = r.message.redirect_url;
				}
			},
		});
	});
};
