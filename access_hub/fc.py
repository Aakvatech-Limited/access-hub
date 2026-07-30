"""Frappe Cloud (press) integration: discover FC sites running access-guard and
enrol them. Endpoints + gotchas: docs/FC_API.md.

Never log a press config response — it echoes the site's secrets (FC_API §6).
"""

import frappe
from frappe.frappeclient import FrappeClient, FrappeException
from frappe.utils import now_datetime

from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL
from access_hub.protocol import (
	GUARD_CONF_HUB_PUBLIC_KEY,
	GUARD_CONF_HUB_URL,
	GUARD_CONF_TRANSPORT_TOKEN,
)

FC_BASE_URL = "https://cloud.frappe.io"
FC_TEAM_HEADER = "X-Press-Team"
GUARD_APP = "access_guard"

NO_LIMIT = 0  # press's `limit` defaults to 20; 0 means all


class FrappeCloudClient(FrappeClient):
	"""Thin press client, shaped after fc_mis.utils.FrappeCloudClient. POST-with-JSON
	throughout: press answers a GET with a bare AttributeError."""

	def __init__(self, settings=None):
		settings = settings or get_settings()
		if not is_enabled(settings):
			frappe.throw("Frappe Cloud integration is not enabled and configured.")

		super().__init__(
			FC_BASE_URL,
			api_key=settings.fc_api_key,
			api_secret=settings.get_password("fc_api_secret"),
		)
		self.headers[FC_TEAM_HEADER] = settings.fc_team_name

	def post_process(self, response):
		"""On a non-JSON body (a 502/504 from press's gateway, an empty response,
		a plain "Internal Server Error") the base client's ValueError just tells
		you the JSON decoder failed at char 0 - useless once it's several hops
		removed in a log. Fold in the status and a body snippet instead; safe to
		log, since a decode failure means this isn't the config-echoing success
		response that FC_API §6 bans logging."""
		try:
			return super().post_process(response)
		except ValueError:
			snippet = response.text[:500].strip() or "<empty body>"
			raise FrappeException(
				f"press returned a non-JSON response (HTTP {response.status_code}): {snippet}"
			) from None

	def _get_list(self, doctype: str, **kwargs) -> list:
		result = self.post_api("press.api.client.get_list", json={"doctype": doctype, **kwargs})
		return result or []  # press returns None for a swallowed error

	def _run_site_method(self, site_id: str, method: str, args: dict | None = None):
		return self.post_api(
			"press.api.client.run_doc_method",
			json={"dt": "Site", "dn": site_id, "method": method, "args": args},
		)

	def fetch_sites(self) -> list:
		"""`[{name, host_name}, ...]`. `name` is the press site ID (addresses every
		other call); `host_name` is the domain users visit. They differ when a custom
		domain is set."""
		return self._get_list("Site", fields=["name", "host_name"], limit=NO_LIMIT)

	def installed_apps(self, site_id: str) -> set:
		"""App names on one site. One call per site - `parent` can't be batched
		(FC_API §2)."""
		rows = self._get_list(
			"Site App",
			fields=["app"],
			filters={"parenttype": "Site", "parent": site_id},
			limit=NO_LIMIT,
		)
		return {row["app"] for row in rows if row.get("app")}

	def guard_enrollment(self, site_id: str) -> dict:
		"""`{"hub_key": ..., "token_hash": ...}` - the guard config already on the
		site, answering "whose site is this" and "does its token match our row".
		The token plaintext is hashed on the spot, never returned or stored."""
		from access_hub.protocol import transport_token_hash

		rows = self._get_list(
			"Site Config",
			fields=["key", "value"],
			filters={"parenttype": "Site", "parent": site_id},
			limit=NO_LIMIT,
		)
		config = {row.get("key"): (row.get("value") or "").strip() for row in rows if row.get("key")}
		token = config.get(GUARD_CONF_TRANSPORT_TOKEN)
		return {
			"hub_key": config.get(GUARD_CONF_HUB_PUBLIC_KEY) or None,
			"token_hash": transport_token_hash(token) if token else None,
		}

	def update_config(self, site_id: str, config: dict):
		"""Merge `config` into the site's site_config.json - press preserves its other
		keys, so we submit only ours. Async: a success means queued, not written, so
		callers must not treat it as confirmation."""
		return self._run_site_method(site_id, "update_config", {"config": config})

	def delete_config(self, site_id: str, key: str):
		return self._run_site_method(site_id, "delete_config", {"key": key})


def get_settings():
	return frappe.get_cached_doc("Access Hub Settings")


def is_enabled(settings=None) -> bool:
	"""Enabled *and* configured - unticking pauses without discarding credentials,
	but a ticked box with no key is still unusable.

	An existence check only, so the secret's dummy "*****" is fine - the real value
	is fetched (and must exist) where the client is built."""
	settings = settings or get_settings()
	if not settings.enable_fc_integration:
		return False
	return bool(settings.fc_team_name and settings.fc_api_key and settings.fc_api_secret)


def can_push_config(site) -> bool:
	"""Whether this hub can deliver guard config to `site` itself, rather than an
	operator copying it by hand."""
	if not (site.is_fc_site and site.fc_site_id):
		return False
	settings = get_settings()
	return bool(settings.fc_auto_enroll_sites) and is_enabled(settings)


def push_guard_config(site, config: dict, client: FrappeCloudClient | None = None) -> None:
	"""Write `config` to the site's site_config.json via press. Raises if press
	won't take it, so callers can avoid recording a token the site never got."""
	client = client or FrappeCloudClient()
	client.update_config(site.fc_site_id, config)
	# Delivered, so there is nothing for an operator to copy: drop the flag that
	# would otherwise make onload surface a copy dialog. A site we could NOT push
	# to keeps it, which is the whole point - its token is only recoverable there.
	site.flags.pop("new_token_plaintext", None)


# --- Discovery ---------------------------------------------------------------


def sync_sites(client: FrappeCloudClient | None = None) -> dict:
	"""Register every FC site running access-guard as a Remote Site, access
	disabled. Errors are per-site: one bad site logs and is skipped, never
	aborting the sweep."""
	client = client or FrappeCloudClient()
	summary = {
		"seen": 0,
		"with_guard": 0,
		"created": 0,
		"updated": 0,
		"token_matched": 0,
		"other_hub": 0,
		"departed": 0,
		"failed": 0,
		# Names of rows this sweep created - the only sites enrollment may enable.
		"created_names": set(),
	}

	sites = client.fetch_sites()
	summary["seen"] = len(sites)
	our_public_key = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
	existing = _existing_rows()  # one query, not two lookups per site
	# Every site the listing accounted for, whether or not we adopted it - a site
	# belonging to another hub is still present on FC, so it hasn't departed.
	seen_fc_ids = set()

	for site in sites:
		site_id = site.get("name")
		if not site_id:
			frappe.log_error(title="access_hub: FC site with no name in list response")
			summary["failed"] += 1
			continue

		try:
			if GUARD_APP not in client.installed_apps(site_id):
				continue
			summary["with_guard"] += 1
			seen_fc_ids.add(site_id)
			enrolment = client.guard_enrollment(site_id)

			# A site enrolled against another hub is not ours to touch: writing our
			# config would overwrite that hub's token and cut off whoever actually
			# issues its access. No key yet just means a new site.
			if enrolment["hub_key"] and enrolment["hub_key"] != our_public_key:
				summary["other_hub"] += 1
				continue

			# host_name is unset until a site first reaches Active; the site ID is
			# itself a reachable domain.
			host = site.get("host_name") or site_id
			created_name, matched = _upsert_remote_site(site_id, host, existing, enrolment)
			summary["created" if created_name else "updated"] += 1
			if created_name:
				summary["created_names"].add(created_name)
			if matched:
				summary["token_matched"] += 1
		except Exception:
			summary["failed"] += 1
			frappe.log_error(
				title=f"access_hub: FC sync failed for {site_id}", message=frappe.get_traceback()
			)

	if summary["other_hub"]:
		# An operator waiting for a site to appear needs to know it was skipped.
		frappe.log_error(
			title="access_hub: FC sites skipped (enrolled against another hub)",
			message=f"{summary['other_hub']} site(s) already carry a different {GUARD_CONF_HUB_PUBLIC_KEY}.",
		)

	# Only when the listing itself succeeded: an empty/partial `sites` from a bad
	# response must not be read as "every site left FC".
	if sites and not summary["failed"]:
		summary["departed"] = _release_departed_sites(seen_fc_ids)

	return summary


def _release_departed_sites(seen_fc_ids: set) -> int:
	"""Clear the FC fields of rows whose site is no longer in the team's listing.
	The only thing that can un-tick read_only is_fc_site, so without this a departed
	row demands an fc_site_id forever. Row and access_disabled are kept.

	`seen_fc_ids` is everything the listing accounted for, including sites we didn't
	adopt (another hub's) - those are still on FC, so they are not departed."""
	departed = [
		row.name
		for row in frappe.get_all("Remote Site", filters={"is_fc_site": 1}, fields=["name", "fc_site_id"])
		if row.fc_site_id not in seen_fc_ids
	]
	for name in departed:
		doc = frappe.get_doc("Remote Site", name)
		doc.update({"is_fc_site": 0, "fc_site_id": None, "fc_team": None, "fc_enrolled_at": None})
		doc.save(ignore_permissions=True)

	if departed:
		frappe.log_error(
			title="access_hub: FC sites no longer listed",
			message=f"Cleared FC fields on {len(departed)} site(s); rows and access state kept.",
		)
	return len(departed)


def _existing_rows() -> dict:
	"""The local registry, indexed for matching without a query per FC site."""
	rows = frappe.get_all("Remote Site", fields=["name", "host", "fc_site_id"])
	return {
		"by_fc_id": {row.fc_site_id: row.name for row in rows if row.fc_site_id},
		# Unclaimed rows only - a row another FC site owns must never be re-pointed.
		"by_host": {row.host: row.name for row in rows if not row.fc_site_id},
	}


def _upsert_remote_site(site_id: str, host: str, existing: dict, enrolment: dict) -> tuple:
	"""Create or refresh the Remote Site for `site_id`, returning
	`(name if created else None, token_matched)`.

	Matched on fc_site_id, not host: host follows the FC custom domain, so matching
	on it would duplicate the row (or trip host's unique constraint) the first time
	that changed. fc_team is stamped by RemoteSite.validate(). Never touches
	access_disabled or a matching token."""
	name = existing["by_fc_id"].get(site_id)

	# Adopt a hand-made row rather than duplicating: whoever created it may have
	# used either identifier as the host. by_host is unclaimed rows only, so this
	# can't steal another site's registration.
	if not name:
		for candidate in (host, site_id):
			name = existing["by_host"].get(candidate)
			if name:
				existing["by_host"].pop(candidate, None)
				break

	if name:
		existing["by_fc_id"][site_id] = name
		doc = frappe.get_doc("Remote Site", name)
		doc.host = host
		doc.fc_site_id = site_id
		doc.is_fc_site = 1
		matched = _reconcile_enrollment(doc, enrolment)
		doc.save(ignore_permissions=True)
		return None, matched

	doc = frappe.new_doc("Remote Site")
	doc.update(
		{
			"host": host,
			"fc_site_id": site_id,
			"is_fc_site": 1,
			# Access starts off. Discovery registers a site; a human or a
			# confirmed enrollment is what makes it usable.
			"access_disabled": 1,
			"port": 443,
			"protocol": "https",
		}
	)
	doc.insert(ignore_permissions=True)
	existing["by_fc_id"][site_id] = doc.name
	return doc.name, False


def _reconcile_enrollment(doc, enrolment: dict) -> bool:
	"""Stamp or clear `doc.fc_enrolled_at` from the config on the site; True if the
	deployed token is the one on file.

	A match means already enrolled - don't rotate. Anything else clears the stamp so
	enrollment re-pushes, which is what repairs a site whose config was deleted
	(enrollment filters on that field).

	Adopting the deployed token instead would authenticate fine and save a write,
	but leaves the hub holding a token it can't reproduce: rotation needs a
	plaintext, and press only hands one back for FC sites. Re-pushing keeps one rule
	for both kinds of site and guarantees we always know what we issued.

	Gated on fc_auto_enroll_sites, the "leave this site's config alone" switch."""
	if (
		enrolment.get("hub_key")
		and enrolment.get("token_hash")
		and enrolment["token_hash"] == doc.transport_token_hash
	):
		# Recognised, so don't re-push. Never touches access_disabled: a site we
		# merely recognised may be disabled on purpose.
		if not doc.fc_enrolled_at:
			doc.fc_enrolled_at = now_datetime()
		return True

	# Not verifiable: config absent, partial, or holding an unknown token. Drop
	# any stamp so this site is picked up for re-enrollment.
	if get_settings().fc_auto_enroll_sites:
		doc.fc_enrolled_at = None
	return False


# --- Enrollment --------------------------------------------------------------


def enrol_pending_sites(client: FrappeCloudClient | None = None, newly_created: set | None = None) -> dict:
	"""Push guard config to every FC Remote Site not yet enrolled and stamp it -
	press accepting the config is authoritative (it applies via an Agent Job, and the
	next sweep reconciles if it never landed).

	Access is enabled only for sites in `newly_created` (what discovery just
	registered); a repair or an adopted row keeps the access state it had."""
	client = client or FrappeCloudClient()
	summary = {"submitted": 0, "failed": 0}
	enable = set(newly_created or ())

	pending = frappe.get_all(
		"Remote Site",
		filters={"is_fc_site": 1, "fc_site_id": ["is", "set"], "fc_enrolled_at": ["is", "not set"]},
		pluck="name",
	)

	for name in pending:
		try:
			site = frappe.get_doc("Remote Site", name)
			config = site.rotate_and_build_config()
			push_guard_config(site, config, client)
			# Only after press accepted it: a failed push must not leave the hub
			# expecting a token the site never received.
			site.fc_enrolled_at = now_datetime()
			if site.access_disabled and name in enable:
				site.access_disabled = 0
			site.save(ignore_permissions=True)
			summary["submitted"] += 1
		except Exception:
			summary["failed"] += 1
			# Traceback only - never the press response body (FC_API §6).
			frappe.log_error(
				title=f"access_hub: FC enrollment failed for {name}", message=frappe.get_traceback()
			)

	return summary


# --- Scheduler / manual trigger ----------------------------------------------


def run_sync() -> dict:
	"""Discovery then enrollment; entry point for the daily job and Sync Now.

	With fc_auto_enroll_sites off it is pull-only: sites get registered with access
	disabled and no client config is written or repaired."""
	settings = get_settings()
	if not is_enabled(settings):
		return {"skipped": "not enabled"}

	client = FrappeCloudClient(settings)
	result = {}

	if settings.fc_auto_pull_sites:
		result["discovery"] = sync_sites(client)
	if settings.fc_auto_enroll_sites:
		# Only sites this run just registered may be enabled on enrollment.
		created = (result.get("discovery") or {}).get("created_names")
		result["enrollment"] = enrol_pending_sites(client, newly_created=created)

	return result


def daily() -> None:
	"""scheduler_events.daily_long. Daily because installing access-guard on a site
	is a rare event; Sync Now covers anyone waiting."""
	if not is_enabled():
		return
	try:
		run_sync()
	except Exception:
		frappe.log_error(title="access_hub: FC daily sync failed", message=frappe.get_traceback())


@frappe.whitelist()
def sync_now() -> str:
	"""Backs the Sync Now button. Enqueued, not inline: a full sweep is ~36s for
	100+ sites (one unbatchable press call each)."""
	frappe.get_single("Access Hub Settings").check_permission("write")
	if not is_enabled():
		frappe.throw("Frappe Cloud integration is not enabled and configured.")

	frappe.enqueue(run_sync, queue="long")
	return "queued"
