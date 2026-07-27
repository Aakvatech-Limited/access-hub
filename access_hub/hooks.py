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
		# after_delete, not on_trash: on_trash fires before the link-existence
		# check that can still reject the delete (a User with linked Access
		# Grant rows, deleted without force) - a rejected delete must not
		# still trigger an eviction push.
		"after_delete": "access_hub.user_events.after_delete",
	}
}

# Access Grant deliberately keeps a dangling Link to a deleted User (the
# audit trail must survive the employee being removed / their email
# changing) - so a User delete must not be blocked by, or cascade to,
# Access Grant rows referencing it.
ignore_links_on_delete = ["Access Grant"]
