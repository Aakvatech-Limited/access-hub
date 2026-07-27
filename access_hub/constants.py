"""Hub-only constants — not part of the wire contract, so not vendored to
access_guard (contrast with protocol.py, which IS vendored verbatim)."""

# --- site_config.json keys (hub side) ---------------------------------------
CONF_HUB_PRIVATE_KEY = "access_hub_private_key"  # PASETO v4.public Ed25519 secret (paserk k4.secret)
CONF_HUB_PUBLIC_KEY_LOCAL = "access_hub_public_key"  # paserk k4.public — hub's own copy, for the
# enrollment-help surface (bench command / desk page) so the value survives beyond the one-time
# keygen printout and never needs re-deriving from the secret key.

# --- Grant token minting -----------------------------------------------------
# aud = transport_token_hash(client transport token); exp = iat + GRANT_TTL.
GRANT_TTL_SECONDS_DEFAULT = 45 * 60  # 30-60 min band; URL redemption deadline, NOT session length

TRANSPORT_TOKEN_MIN_LEN = 8
