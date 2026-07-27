app_name = "access_hub"
app_title = "Access Hub"
app_publisher = "Aakvatech Limited"
app_description = "Central issuer for ephemeral Frappe site access"
app_email = "info@aakvatech.com"
app_license = "mit"

require_type_annotated_api_methods = True

after_install = "access_hub.setup.after_install"
after_migrate = "access_hub.setup.after_migrate"
