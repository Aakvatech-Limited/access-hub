"""PASETO v4.public signing helpers for access_hub.

The hub holds the private key (site_config.json: access_hub_private_key,
paserk k4.secret...). All signed artifacts (grant tokens, policy tokens) go
through here so key-loading logic lives in exactly one place.
"""

import frappe
from pyseto import Key

from access_hub.constants import CONF_HUB_PRIVATE_KEY
from access_hub.protocol import ClientNotConfiguredError


def get_private_key() -> Key:
	secret_paserk = frappe.conf.get(CONF_HUB_PRIVATE_KEY)
	if not secret_paserk:
		raise ClientNotConfiguredError(
			f"{CONF_HUB_PRIVATE_KEY} is not set. Run `bench access-hub-generate-keypair`."
		)
	return Key.from_paserk(secret_paserk)


def sign_paseto(claims: dict) -> str:
	"""Sign `claims` as a PASETO v4.public token with the hub's private key."""
	import pyseto

	key = get_private_key()
	token = pyseto.encode(key, payload=claims)
	return token.decode("utf-8") if isinstance(token, bytes) else token
