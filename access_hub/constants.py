"""Hub-only constants — not part of the wire contract, so not vendored to
access_guard (contrast with protocol.py, which IS vendored verbatim)."""

# --- site_config.json keys (hub side) ---------------------------------------
CONF_HUB_PRIVATE_KEY = "access_hub_private_key"  # PASETO v4.public Ed25519 secret (paserk k4.secret)
CONF_HUB_PUBLIC_KEY_LOCAL = "access_hub_public_key"  # paserk k4.public — hub's own copy, for the
# enrollment-help surface (bench command / desk page) so the value survives beyond the one-time
# keygen printout and never needs re-deriving from the secret key.

# --- Grant token minting -----------------------------------------------------
# aud = transport_token_hash(client transport token); exp = iat + GRANT_TTL.
GRANT_TTL_SECONDS_DEFAULT = 120  # URL redemption deadline, NOT session length - browser redirects immediately

# --- Log ingest ---------------------------------------------------------------
# Per-call cap on push_logs' `lines`. The hub is the trusted component whose
# availability every tenant depends on, and append_lines writes whatever arrives
# to disk - so one compromised or looping client must not be able to fill the
# issuer's disk. Generous next to a real session's hourly tail (guard-side
# max_key_size_bytes caps individual values at 256 KiB, and .doc.log lines are
# size-exempt), so a legitimate push is never rejected.
MAX_PUSH_BYTES = 8 * 1024 * 1024

# --- Policy floors ------------------------------------------------------------
# Lower bound on session_expiry_seconds, enforced in AccessHubSettings.validate.
# Guards against 0 ("never idle out"), which Frappe's reqd/non_negative both
# allow on an Int field and which would otherwise be signed into a policy - see
# the guard's IDLE_EXPIRY_FALLBACK_SECONDS for the client-side backstop.
MIN_SESSION_EXPIRY_SECONDS = 60

# --- Policy token minting -----------------------------------------------------
# exp exists only because verify_and_decode() requires it on every token,
# uniformly - not because a policy token needs its own staleness window (it's
# re-signed on every cache miss, see get_current_policy_token). A few days
# comfortably outlasts POLICY_TOKEN_CACHE_TTL_SECONDS so a cached token is
# never expired.
POLICY_TOKEN_TTL_DAYS = 3
# A Settings save busts this immediately (republish_policy) regardless of TTL;
# the TTL only bounds staleness for anything that skips that path.
POLICY_TOKEN_CACHE_TTL_SECONDS = 24 * 60 * 60
POLICY_TOKEN_CACHE_KEY = "access_hub_current_policy_token"

TRANSPORT_TOKEN_MIN_LEN = 8
