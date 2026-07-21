"""Bench CLI commands for access_hub.

Registered via `commands = [...]` — discovered by frappe's bench_helper as
`access_hub.commands`.
"""

import click
import frappe


def _generate_paserk_keypair():
	"""Generate a fresh Ed25519 PASETO v4.public keypair.

	Returns (secret_paserk, public_paserk) as strings, e.g.
	("k4.secret....", "k4.public....").
	"""
	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from pyseto import Key

	sk = Ed25519PrivateKey.generate()

	priv_pem = sk.private_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PrivateFormat.PKCS8,
		encryption_algorithm=serialization.NoEncryption(),
	)
	pub_pem = sk.public_key().public_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PublicFormat.SubjectPublicKeyInfo,
	)

	priv_key = Key.new(version=4, purpose="public", key=priv_pem)
	pub_key = Key.new(version=4, purpose="public", key=pub_pem)

	return priv_key.to_paserk(), pub_key.to_paserk()


@click.command("access-hub-generate-keypair")
@click.option(
	"--force",
	is_flag=True,
	default=False,
	help="Overwrite an existing access_hub_private_key in site_config.json.",
)
@click.option("--site", help="Site to operate on (defaults to the bench-selected site).")
@click.pass_context
def generate_keypair(ctx, force, site):
	"""Generate the hub's Ed25519 PASETO keypair.

	Writes the secret key (k4.secret...) to site_config.json under
	`access_hub_private_key` and prints the public key (k4.public...) for the
	operator to paste into each client's `access_guard_hub_public_key` config.

	Idempotent: refuses to overwrite an existing key unless --force is passed.
	"""
	site = site or (ctx.obj["sites"][0] if ctx.obj.get("sites") else None)
	if not site:
		click.secho("No site specified. Use --site <sitename>.", fg="red")
		raise click.exceptions.Exit(1)

	frappe.init(site=site)
	frappe.connect()
	try:
		from access_hub.protocol import CONF_HUB_PRIVATE_KEY, CONF_HUB_PUBLIC_KEY_LOCAL

		existing = frappe.conf.get(CONF_HUB_PRIVATE_KEY)
		if existing and not force:
			click.secho(
				f"{CONF_HUB_PRIVATE_KEY} already set in site_config.json. "
				"Refusing to overwrite. Pass --force to regenerate. "
				"Run `bench access-hub-enroll-info` to see the current public key again.",
				fg="red",
			)
			raise click.exceptions.Exit(1)

		secret_paserk, public_paserk = _generate_paserk_keypair()

		from frappe.installer import update_site_config

		update_site_config(CONF_HUB_PRIVATE_KEY, secret_paserk)
		update_site_config(CONF_HUB_PUBLIC_KEY_LOCAL, public_paserk)

		click.secho("Keypair generated.", fg="green")
		click.secho(f"Private key written to site_config.json ({CONF_HUB_PRIVATE_KEY}).", fg="green")
		click.secho(f"Public key written to site_config.json ({CONF_HUB_PUBLIC_KEY_LOCAL}).", fg="green")
		click.secho("Public key (paste into each client's access_guard_hub_public_key):", fg="yellow")
		click.echo(public_paserk)
		click.secho("Run `bench access-hub-enroll-info` any time to see full enrollment info again.", fg="cyan")
	finally:
		frappe.destroy()


@click.command("access-hub-enroll-info")
@click.option("--site", help="Site to operate on (defaults to the bench-selected site).")
@click.pass_context
def enroll_info(ctx, site):
	"""Print everything needed to enroll a new client site with access-guard.

	Shows the hub URL, the hub's public key (paste into the client's
	`access_guard_hub_public_key`), and reminds the operator that a per-client
	transport token is minted from the Remote Site doctype (see its "Get
	Enrollment Info" action for the full three-value snippet for one client).
	"""
	site = site or (ctx.obj["sites"][0] if ctx.obj.get("sites") else None)
	if not site:
		click.secho("No site specified. Use --site <sitename>.", fg="red")
		raise click.exceptions.Exit(1)

	frappe.init(site=site)
	frappe.connect()
	try:
		from access_hub.protocol import CONF_HUB_PUBLIC_KEY_LOCAL

		public_paserk = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
		if not public_paserk:
			click.secho(
				"No keypair found. Run `bench access-hub-generate-keypair` first.",
				fg="red",
			)
			raise click.exceptions.Exit(1)

		hub_url = frappe.utils.get_url()

		click.secho("Access Hub enrollment info", fg="green", bold=True)
		click.echo("")
		click.secho("access_guard_hub_url:", fg="yellow")
		click.echo(f"  {hub_url}")
		click.secho("access_guard_hub_public_key:", fg="yellow")
		click.echo(f"  {public_paserk}")
		click.echo("")
		click.echo(
			"A per-client access_guard_transport_token is minted per Remote Site — "
			"create or open a Remote Site record on the hub desk and use its "
			'"Get Enrollment Info" action for the full snippet for that client.'
		)
	finally:
		frappe.destroy()


commands = [generate_keypair, enroll_info]
