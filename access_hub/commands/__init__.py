"""Bench CLI commands for access_hub.

Registered via `commands = [...]` — discovered by frappe's bench_helper as
`access_hub.commands`.
"""

import click
import frappe
from frappe.commands import get_site, pass_context


@click.command("access-hub-generate-keypair")
@click.option(
	"--force",
	is_flag=True,
	default=False,
	help="Overwrite an existing access_hub_private_key in site_config.json.",
)
@pass_context
def generate_keypair(context, force):
	"""Generate the hub's Ed25519 PASETO keypair.

	Writes the secret key (k4.secret...) to site_config.json under
	`access_hub_private_key` and prints the public key (k4.public...) for the
	operator to paste into each client's `access_guard_hub_public_key` config.

	Idempotent: refuses to overwrite an existing key unless --force is passed.
	"""
	# --site is a bench-level option, so it lands in the group's context, not
	# ours. get_site raises SiteNotSpecifiedError, which frappe's pass_context
	# turns into a friendly message (as it does for a bad site path).
	site = get_site(context)

	frappe.init(site=site)
	frappe.connect()
	try:
		from access_hub.constants import CONF_HUB_PRIVATE_KEY, CONF_HUB_PUBLIC_KEY_LOCAL

		if frappe.conf.get(CONF_HUB_PRIVATE_KEY) and not force:
			click.secho(
				f"{CONF_HUB_PRIVATE_KEY} already set in site_config.json. "
				"Refusing to overwrite. Pass --force to regenerate.",
				fg="red",
			)
			raise click.exceptions.Exit(1)

		from access_hub.utils.crypto import ensure_keypair

		public_paserk = ensure_keypair(force=force)

		click.secho("Keypair generated.", fg="green")
		click.secho(f"Private key written to site_config.json ({CONF_HUB_PRIVATE_KEY}).", fg="green")
		click.secho(f"Public key written to site_config.json ({CONF_HUB_PUBLIC_KEY_LOCAL}).", fg="green")
		click.secho("Public key (paste into each client's access_guard_hub_public_key):", fg="yellow")
		click.echo(public_paserk)
	finally:
		frappe.destroy()


commands = [generate_keypair]
