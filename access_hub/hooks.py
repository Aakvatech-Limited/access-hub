app_name = "access_hub"
app_title = "Access Hub"
app_publisher = "Aakvatech Limited"
app_description = "Central issuer for ephemeral Frappe site access"
app_email = "info@aakvatech.com"
app_license = "mit"

require_type_annotated_api_methods = True

after_install = "access_hub.setup.after_install"
after_migrate = "access_hub.setup.after_migrate"

doc_events = {
	"User": {
		"on_update": "access_hub.user_events.on_update",
		"on_trash": "access_hub.user_events.on_trash",
	}
}

# Access Grant deliberately keeps a dangling Link to a deleted User (the
# audit trail must survive the user being removed / their email
# changing) - so a User delete must not be blocked by, or cascade to,
# Access Grant rows referencing it.
ignore_links_on_delete = ["Access Grant"]
