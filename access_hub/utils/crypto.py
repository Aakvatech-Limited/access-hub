"""PASETO v4.public signing helpers for access_hub.

The hub holds the private key (site_config.json: access_hub_private_key,
paserk k4.secret...). All signed artifacts (grant tokens, policy tokens) go
through here so key-loading logic lives in exactly one place.
"""

import frappe
from pyseto import Key

from access_hub.constants import CONF_HUB_PRIVATE_KEY, CONF_HUB_PUBLIC_KEY_LOCAL
from access_hub.protocol import ClientNotConfiguredError


def get_private_key() -> Key:
	secret_paserk = frappe.conf.get(CONF_HUB_PRIVATE_KEY)
	if not secret_paserk:
		raise ClientNotConfiguredError(
			f"{CONF_HUB_PRIVATE_KEY} is not set. Run `bench access-hub-generate-keypair`."
		)
	return Key.from_paserk(secret_paserk)


def generate_keypair_paserk() -> tuple:
	"""Generate a fresh Ed25519 PASETO v4.public keypair.

	Returns (secret_paserk, public_paserk), e.g. ("k4.secret...", "k4.public...").
	"""
	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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


def ensure_keypair(force: bool = False) -> str | None:
	"""Generate and persist a fresh keypair to site_config.json (both the secret
	and the hub's own public copy). The single writer of these keys.

	Idempotent by default — a no-op returning None when a private key is already
	set; pass `force=True` to overwrite (deliberate rotation). Returns the public
	paserk (`k4.public...`) when a keypair was written, else None.
	"""
	if frappe.conf.get(CONF_HUB_PRIVATE_KEY) and not force:
		return None
	from frappe.installer import update_site_config

	secret_paserk, public_paserk = generate_keypair_paserk()
	update_site_config(CONF_HUB_PRIVATE_KEY, secret_paserk)
	update_site_config(CONF_HUB_PUBLIC_KEY_LOCAL, public_paserk)
	return public_paserk


def sign_paseto(claims: dict) -> str:
	"""Sign `claims` as a PASETO v4.public token with the hub's private key."""
	import pyseto

	key = get_private_key()
	token = pyseto.encode(key, payload=claims)
	return token.decode("utf-8") if isinstance(token, bytes) else token
