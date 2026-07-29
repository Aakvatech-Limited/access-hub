# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

"""Frappe Cloud integration. No test hits the real press API - FakeFCClient
stands in with the same surface, so these exercise our own discovery/enrollment
logic. Real request/response shapes: docs/FC_API.md.
"""

from unittest.mock import patch

import frappe

from access_hub import fc
from access_hub.tests.test_protocol_flows import HubTestCase


class FakeFCClient:
	"""In-memory press. `sites` is `{site_id: {host_name, apps, hub_key,
	token_hash}}` - hub_key/token_hash being the guard config already on the site.
	"""

	def __init__(self, sites=None):
		self.sites = sites or {}
		self.config_writes = []
		self.installed_apps_calls = []

	def fetch_sites(self):
		return [{"name": sid, "host_name": data.get("host_name")} for sid, data in self.sites.items()]

	def installed_apps(self, site_id):
		self.installed_apps_calls.append(site_id)
		return set(self.sites[site_id].get("apps") or [])

	def guard_enrollment(self, site_id):
		data = self.sites[site_id]
		return {"hub_key": data.get("hub_key"), "token_hash": data.get("token_hash")}

	def update_config(self, site_id, config):
		# Keys + a token hash only: a failing assertion prints whatever this holds,
		# so it must never hold a secret.
		from access_hub.protocol import transport_token_hash

		token = config.get("access_guard_transport_token")
		token_hash = transport_token_hash(token) if token else None
		self.config_writes.append((site_id, {"keys": set(config), "token_hash": token_hash}))

		# Merge, as press does, so a later sweep can read back what was pushed.
		site = self.sites.setdefault(site_id, {})
		if "access_guard_hub_public_key" in config:
			site["hub_key"] = config["access_guard_hub_public_key"]
		if token_hash:
			site["token_hash"] = token_hash
		return {"docs": [{"name": site_id}]}

	def delete_config(self, site_id, key):
		self.config_writes.append((site_id, {"deleted": key}))
		return {"docs": [{"name": site_id}]}

	def writes_for(self, site_id):
		"""Submissions for one site: enrol_pending_sites sweeps every unenrolled row
		in the DB, including ones a test didn't create."""
		return [config for sid, config in self.config_writes if sid == site_id]


class FCTestCase(HubTestCase):
	"""Enables FC integration per test, then restores shipped defaults. Commits,
	for the reason HubTestCase documents."""

	def setUp(self):
		super().setUp()
		self.addCleanup(self._restore_fc_settings)

	def _restore_fc_settings(self):
		"""Restore shipped defaults, not whatever the last test left: capturing "the
		original" from an already-dirtied Single just propagates the dirt. Off first,
		in its own write, to satisfy validate()'s credentials-while-enabled rule."""
		settings = frappe.get_single("Access Hub Settings")
		settings.enable_fc_integration = 0
		settings.save(ignore_permissions=True)

		settings.update(
			{
				"fc_team_name": None,
				"fc_api_key": None,
				"fc_api_secret": None,
				"fc_auto_pull_sites": 1,
				"fc_auto_enroll_sites": 1,
			}
		)
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype="Access Hub Settings")

	def enable_fc(self, **overrides):
		settings = frappe.get_single("Access Hub Settings")
		settings.update(
			{
				"enable_fc_integration": 1,
				"fc_team_name": "team@example.com",
				"fc_api_key": "fake-key",
				"fc_api_secret": "fake-secret",
				"fc_auto_pull_sites": 1,
				"fc_auto_enroll_sites": 1,
				**overrides,
			}
		)
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		# fc.get_settings reads the cached doc.
		frappe.clear_cache(doctype="Access Hub Settings")
		return settings

	def cleanup_fc_site(self, site_id, host=None):
		"""Delete the row for `site_id` afterwards. Also matches on host, since a
		released site (§ departed) has had its fc_site_id cleared and would
		otherwise be unfindable."""

		def _delete():
			filters = {"fc_site_id": site_id}
			name = frappe.db.get_value("Remote Site", filters, "name")
			if not name and host:
				name = frappe.db.get_value("Remote Site", {"host": host}, "name")
			if name:
				frappe.delete_doc("Remote Site", name, ignore_permissions=True, force=True)
				frappe.db.commit()

		self.addCleanup(_delete)


class TestSettingsValidation(FCTestCase):
	def test_enabling_without_credentials_is_refused(self):
		settings = frappe.get_single("Access Hub Settings")
		settings.enable_fc_integration = 1
		settings.fc_team_name = None
		settings.fc_api_key = None
		settings.fc_api_secret = None

		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_disabling_keeps_credentials(self):
		"""The whole point of a real checkbox over a derived flag: pausing the
		integration must not force an operator to re-enter the secret."""
		self.enable_fc()

		settings = frappe.get_single("Access Hub Settings")
		settings.enable_fc_integration = 0
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype="Access Hub Settings")

		settings.reload()
		self.assertEqual(settings.fc_team_name, "team@example.com")
		self.assertEqual(settings.fc_api_key, "fake-key")
		self.assertEqual(settings.get_password("fc_api_secret", raise_exception=False), "fake-secret")
		self.assertFalse(fc.is_enabled(), "disabled means disabled even with credentials present")

	def test_is_enabled_false_without_secret(self):
		"""Ticked but unconfigured must read as off, not raise: is_enabled gates a
		scheduled job. db_set bypasses validate(), which is how such a row exists."""
		settings = frappe.get_single("Access Hub Settings")
		settings.db_set("enable_fc_integration", 1, update_modified=False)
		settings.db_set("fc_team_name", "team@example.com", update_modified=False)
		settings.db_set("fc_api_key", "fake-key", update_modified=False)
		settings.db_set("fc_api_secret", None, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype="Access Hub Settings")

		self.assertFalse(fc.is_enabled())

	def test_run_sync_noop_when_disabled(self):
		settings = frappe.get_single("Access Hub Settings")
		settings.db_set("enable_fc_integration", 0, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype="Access Hub Settings")

		self.assertEqual(fc.run_sync(), {"skipped": "not enabled"})


class TestDiscovery(FCTestCase):
	def test_only_guard_sites_are_registered(self):
		self.enable_fc()
		client = FakeFCClient(
			{
				"guarded.frappe.cloud": {"host_name": "erp.guarded.com", "apps": ["frappe", "access_guard"]},
				"plain.frappe.cloud": {"host_name": "plain.example.com", "apps": ["frappe", "erpnext"]},
			}
		)
		self.cleanup_fc_site("guarded.frappe.cloud")
		self.cleanup_fc_site("plain.frappe.cloud")

		summary = fc.sync_sites(client)

		self.assertEqual(summary["seen"], 2)
		self.assertEqual(summary["with_guard"], 1)
		self.assertEqual(summary["created"], 1)
		self.assertTrue(frappe.db.exists("Remote Site", {"fc_site_id": "guarded.frappe.cloud"}))
		self.assertFalse(frappe.db.exists("Remote Site", {"fc_site_id": "plain.frappe.cloud"}))

	def test_new_site_starts_with_access_disabled(self):
		self.enable_fc()
		client = FakeFCClient({"new.frappe.cloud": {"host_name": "erp.new.com", "apps": ["access_guard"]}})
		self.cleanup_fc_site("new.frappe.cloud")

		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "new.frappe.cloud"})
		self.assertTrue(site.access_disabled, "discovery must never create an enabled site")
		self.assertTrue(site.is_fc_site)
		self.assertEqual(site.host, "erp.new.com", "host is the browser-facing domain")
		self.assertEqual(site.fc_site_id, "new.frappe.cloud", "fc_site_id is the press ID")
		self.assertEqual(site.fc_team, "team@example.com")
		self.assertEqual(site.protocol, "https")
		self.assertEqual(site.port, 443)
		self.assertTrue(site.transport_token_hash, "token autogenerated by validate()")

	def test_host_falls_back_to_site_id_when_no_custom_domain(self):
		self.enable_fc()
		client = FakeFCClient({"bare.frappe.cloud": {"host_name": None, "apps": ["access_guard"]}})
		self.cleanup_fc_site("bare.frappe.cloud")

		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "bare.frappe.cloud"})
		self.assertEqual(site.host, "bare.frappe.cloud")

	def test_resync_updates_host_and_never_reenables(self):
		"""A custom domain can change on FC, so the row follows it — but an
		operator's kill switch must survive the next sweep."""
		self.enable_fc()
		self.cleanup_fc_site("moving.frappe.cloud")
		client = FakeFCClient(
			{"moving.frappe.cloud": {"host_name": "old.example.com", "apps": ["access_guard"]}}
		)
		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "moving.frappe.cloud"})
		site.access_disabled = 0  # operator enabled it
		site.save(ignore_permissions=True)

		client.sites["moving.frappe.cloud"]["host_name"] = "new.example.com"
		summary = fc.sync_sites(client)

		self.assertEqual(summary["created"], 0, "must match the existing row, not duplicate it")
		self.assertEqual(summary["updated"], 1)
		site.reload()
		self.assertEqual(site.host, "new.example.com")
		self.assertFalse(site.access_disabled, "sync must not touch the operator's kill switch")

	def test_operator_disabled_site_stays_disabled_across_sweep(self):
		self.enable_fc()
		self.cleanup_fc_site("killed.frappe.cloud")
		client = FakeFCClient(
			{"killed.frappe.cloud": {"host_name": "erp.killed.com", "apps": ["access_guard"]}}
		)
		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "killed.frappe.cloud"})
		site.db_set("fc_enrolled_at", frappe.utils.now_datetime(), update_modified=False)
		site.db_set("access_disabled", 1, update_modified=False)

		fc.sync_sites(client)
		site.reload()
		self.assertTrue(site.access_disabled, "an operator's disable must survive discovery")

	def test_one_bad_site_does_not_abort_the_sweep(self):
		self.enable_fc()
		self.cleanup_fc_site("good.frappe.cloud")

		class Exploding(FakeFCClient):
			def installed_apps(self, site_id):
				if site_id == "bad.frappe.cloud":
					raise RuntimeError("press exploded")
				return super().installed_apps(site_id)

		client = Exploding(
			{
				"bad.frappe.cloud": {"host_name": "bad.example.com", "apps": ["access_guard"]},
				"good.frappe.cloud": {"host_name": "good.example.com", "apps": ["access_guard"]},
			}
		)

		summary = fc.sync_sites(client)

		self.assertEqual(summary["failed"], 1)
		self.assertEqual(summary["created"], 1)
		self.assertTrue(frappe.db.exists("Remote Site", {"fc_site_id": "good.frappe.cloud"}))


class TestEnrollment(FCTestCase):
	def _discover(self, site_id, apps=("access_guard",)):
		"""Discover one site; `client.created` is what run_sync hands enrollment to
		mark a site newly registered."""
		self.cleanup_fc_site(site_id)
		client = FakeFCClient({site_id: {"host_name": f"erp.{site_id}", "apps": list(apps)}})
		client.created = fc.sync_sites(client)["created_names"]
		return client

	def test_submits_the_three_guard_config_keys(self):
		from access_hub.protocol import (
			GUARD_CONF_HUB_PUBLIC_KEY,
			GUARD_CONF_HUB_URL,
			GUARD_CONF_TRANSPORT_TOKEN,
		)

		self.enable_fc()
		client = self._discover("enrol.frappe.cloud")

		fc.enrol_pending_sites(client)

		writes = client.writes_for("enrol.frappe.cloud")
		self.assertEqual(len(writes), 1)
		self.assertEqual(
			writes[0]["keys"],
			{GUARD_CONF_HUB_URL, GUARD_CONF_HUB_PUBLIC_KEY, GUARD_CONF_TRANSPORT_TOKEN},
			"exactly the three guard keys - press merges, so we submit only ours",
		)

		# The token we submitted is the one whose hash is now on the row - proven
		# by comparing hashes, so neither side of the assertion is a plaintext.
		site = frappe.get_doc("Remote Site", {"fc_site_id": "enrol.frappe.cloud"})
		self.assertEqual(site.transport_token_hash, writes[0]["token_hash"])

	def test_enrollment_stamps_and_enables_on_press_success(self):
		"""press accepting the config is authoritative."""
		self.enable_fc()
		client = self._discover("async.frappe.cloud")

		fc.enrol_pending_sites(client, newly_created=client.created)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "async.frappe.cloud"})
		self.assertTrue(site.fc_enrolled_at)
		self.assertFalse(site.access_disabled)

	def test_next_sweep_recognises_the_pushed_config(self):
		"""The following sweep reads its own config back and leaves it alone."""
		self.enable_fc()
		client = self._discover("confirm.frappe.cloud")
		fc.enrol_pending_sites(client, newly_created=client.created)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "confirm.frappe.cloud"})
		stamped_at = site.fc_enrolled_at

		summary = fc.sync_sites(client)

		self.assertEqual(summary["token_matched"], 1)
		site.reload()
		self.assertEqual(site.fc_enrolled_at, stamped_at, "stamp must not move")
		self.assertFalse(site.access_disabled)

	def test_later_sweeps_never_reenable_a_disabled_site(self):
		"""Once enrolled, a sweep must not undo an operator's disable."""
		self.enable_fc()
		client = self._discover("idem.frappe.cloud")
		fc.enrol_pending_sites(client, newly_created=client.created)
		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "idem.frappe.cloud"})
		stamped_at = site.fc_enrolled_at
		self.assertTrue(stamped_at)

		site.access_disabled = 1  # operator kills it afterwards
		site.save(ignore_permissions=True)

		fc.sync_sites(client)
		site.reload()
		self.assertEqual(site.fc_enrolled_at, stamped_at, "stamp must not move")
		self.assertTrue(site.access_disabled, "a later sweep must not re-enable a disabled site")

	def test_already_enrolled_site_is_not_resubmitted(self):
		self.enable_fc()
		client = self._discover("once.frappe.cloud")
		fc.enrol_pending_sites(client)
		self.assertEqual(len(client.writes_for("once.frappe.cloud")), 1)

		fc.enrol_pending_sites(client)
		self.assertEqual(
			len(client.writes_for("once.frappe.cloud")), 1, "enrolled sites are skipped entirely"
		)

	def test_non_fc_site_is_never_enrolled(self):
		self.enable_fc()
		manual = self.make_remote_site(f"manual-{self._testMethodName}.localhost")
		client = FakeFCClient()

		fc.enrol_pending_sites(client)
		self.assertEqual(client.writes_for(manual.name), [], "a hand-made site is not FC's business")
		self.assertEqual(client.writes_for(manual.host), [])

		manual.reload()
		self.assertFalse(manual.fc_enrolled_at)


class TestRemoteSiteFCValidation(FCTestCase):
	"""is_fc_site is only meaningful alongside the identifiers press is addressed
	by, and mandatory_depends_on can't enforce that (client-side only, and both
	fields are read_only). So the controller does."""

	def test_ticking_is_fc_site_without_identifiers_is_refused(self):
		site = self.make_remote_site(f"fcvalid-{self._testMethodName}.localhost")
		site.is_fc_site = 1

		with self.assertRaises(frappe.ValidationError):
			site.save(ignore_permissions=True)

	def test_fc_site_id_is_the_only_required_identifier(self):
		"""fc_team is derived on save, so it is never something a caller must
		supply - the site ID is the one thing that can't be inferred."""
		self.enable_fc(fc_team_name="derived@example.com")
		site = self.make_remote_site(f"fcvalid-partial-{self._testMethodName}.localhost")
		site.is_fc_site = 1
		site.fc_site_id = "partial.frappe.cloud"
		site.save(ignore_permissions=True)

		site.reload()
		self.assertEqual(site.fc_team, "derived@example.com")

	def test_fc_team_is_stamped_from_settings_not_supplied(self):
		"""fc_team is derived on save, so it can't drift from the configured team
		even if a caller passes something else."""
		self.enable_fc(fc_team_name="stamped@example.com")
		self.cleanup_fc_site("stamp.frappe.cloud")
		client = FakeFCClient(
			{"stamp.frappe.cloud": {"host_name": "erp.stamp.com", "apps": ["access_guard"]}}
		)
		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "stamp.frappe.cloud"})
		self.assertEqual(site.fc_team, "stamped@example.com")

		site.fc_team = "someone-else@example.com"
		site.save(ignore_permissions=True)
		site.reload()
		self.assertEqual(site.fc_team, "stamped@example.com", "re-stamped from settings on save")

	def test_fc_values_without_the_flag_are_refused(self):
		"""The inverse: values set but the flag off would hide them behind the
		section's depends_on while enrollment still acted on the row."""
		site = self.make_remote_site(f"fcvalid-inverse-{self._testMethodName}.localhost")
		site.fc_site_id = "orphan.frappe.cloud"
		site.fc_team = "team@example.com"

		with self.assertRaises(frappe.ValidationError):
			site.save(ignore_permissions=True)

	def test_synced_site_saves_cleanly(self):
		"""What discovery produces must pass its own validation on re-save."""
		self.enable_fc()
		self.cleanup_fc_site("valid.frappe.cloud")
		client = FakeFCClient(
			{"valid.frappe.cloud": {"host_name": "erp.valid.com", "apps": ["access_guard"]}}
		)
		fc.sync_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "valid.frappe.cloud"})
		site.save(ignore_permissions=True)  # must not raise
		self.assertTrue(site.is_fc_site)

	def test_non_fc_site_unaffected(self):
		site = self.make_remote_site(f"fcvalid-plain-{self._testMethodName}.localhost")
		site.port = 8006
		site.save(ignore_permissions=True)
		self.assertFalse(site.is_fc_site)


class TestForeignHubSites(FCTestCase):
	"""An FC site already enrolled against another hub must be left alone.
	Adopting it would overwrite that hub's public key and transport token,
	silently cutting off whoever actually issues its access."""

	def our_key(self):
		from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL

		return frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)

	def test_site_enrolled_against_another_hub_is_skipped(self):
		self.enable_fc()
		self.cleanup_fc_site("foreign.frappe.cloud")
		client = FakeFCClient(
			{
				"foreign.frappe.cloud": {
					"host_name": "erp.foreign.com",
					"apps": ["access_guard"],
					"hub_key": "k4.public.SOMEONE_ELSES_HUB_KEY",
				}
			}
		)

		summary = fc.sync_sites(client)

		self.assertEqual(summary["with_guard"], 1)
		self.assertEqual(summary["other_hub"], 1)
		self.assertEqual(summary["created"], 0)
		self.assertFalse(
			frappe.db.exists("Remote Site", {"fc_site_id": "foreign.frappe.cloud"}),
			"another hub's site must never be registered here",
		)

	def test_site_already_enrolled_against_us_is_adopted(self):
		"""Configured with OUR key - e.g. this hub was restored from backup, or
		the row was deleted by hand. Re-adopting is correct."""
		self.enable_fc()
		self.cleanup_fc_site("ours.frappe.cloud")
		client = FakeFCClient(
			{
				"ours.frappe.cloud": {
					"host_name": "erp.ours.com",
					"apps": ["access_guard"],
					"hub_key": self.our_key(),
				}
			}
		)

		summary = fc.sync_sites(client)

		self.assertEqual(summary["other_hub"], 0)
		self.assertEqual(summary["created"], 1)
		site = frappe.get_doc("Remote Site", {"fc_site_id": "ours.frappe.cloud"})
		self.assertTrue(site.access_disabled, "adoption still starts disabled")

	def test_unconfigured_site_is_adopted(self):
		self.enable_fc()
		self.cleanup_fc_site("fresh.frappe.cloud")
		client = FakeFCClient(
			{"fresh.frappe.cloud": {"host_name": "erp.fresh.com", "apps": ["access_guard"], "hub_key": None}}
		)

		summary = fc.sync_sites(client)

		self.assertEqual(summary["other_hub"], 0)
		self.assertEqual(summary["created"], 1)

	def test_foreign_site_is_not_enrolled_either(self):
		"""Skipped at discovery means no row, so enrollment can't reach it."""
		self.enable_fc()
		self.cleanup_fc_site("foreign2.frappe.cloud")
		client = FakeFCClient(
			{
				"foreign2.frappe.cloud": {
					"host_name": "erp.foreign2.com",
					"apps": ["access_guard"],
					"hub_key": "k4.public.NOT_OURS",
				}
			}
		)
		fc.sync_sites(client)

		fc.enrol_pending_sites(client)
		self.assertEqual(
			client.writes_for("foreign2.frappe.cloud"), [], "must not push config to another hub's site"
		)


class TestAdoptingManualSites(FCTestCase):
	"""A site enrolled by hand before FC integration existed (or after a row was
	recreated manually) must be adopted, not duplicated — host is unique, so a
	second row would fail outright."""

	def test_adopts_row_matched_on_host_name(self):
		self.enable_fc()
		self.cleanup_fc_site("adopt.frappe.cloud")
		manual = self.make_remote_site("erp.adopt.com")
		original_hash = manual.transport_token_hash

		client = FakeFCClient(
			{"adopt.frappe.cloud": {"host_name": "erp.adopt.com", "apps": ["access_guard"]}}
		)
		summary = fc.sync_sites(client)

		self.assertEqual(summary["created"], 0, "adopted, not created")
		self.assertEqual(summary["updated"], 1)
		self.assertEqual(frappe.db.count("Remote Site", {"host": "erp.adopt.com"}), 1, "no duplicate")

		manual.reload()
		self.assertTrue(manual.is_fc_site)
		self.assertEqual(manual.fc_site_id, "adopt.frappe.cloud")
		self.assertEqual(
			manual.transport_token_hash, original_hash, "adoption must not rotate a working token"
		)

	def test_adopts_row_matched_on_fc_site_id_as_host(self):
		"""Whoever created it by hand may have used the *.frappe.cloud name as the
		host, since that is also a reachable domain."""
		self.enable_fc()
		self.cleanup_fc_site("byid.frappe.cloud")
		manual = self.make_remote_site("byid.frappe.cloud")

		client = FakeFCClient({"byid.frappe.cloud": {"host_name": "erp.byid.com", "apps": ["access_guard"]}})
		summary = fc.sync_sites(client)

		self.assertEqual(summary["created"], 0)
		manual.reload()
		self.assertEqual(manual.fc_site_id, "byid.frappe.cloud")
		self.assertEqual(manual.host, "erp.byid.com", "host follows FC's custom domain")

	def test_adoption_preserves_enabled_access(self):
		"""An operator had this site live already; adopting it must not disable it."""
		self.enable_fc()
		self.cleanup_fc_site("live.frappe.cloud")
		manual = self.make_remote_site("erp.live.com", access_disabled=0)

		fc.sync_sites(
			FakeFCClient({"live.frappe.cloud": {"host_name": "erp.live.com", "apps": ["access_guard"]}})
		)

		manual.reload()
		self.assertFalse(manual.access_disabled, "adoption must not touch the kill switch either way")

	def test_does_not_steal_a_row_already_claimed_by_another_fc_site(self):
		self.enable_fc()
		self.cleanup_fc_site("first.frappe.cloud")
		self.cleanup_fc_site("second.frappe.cloud")
		fc.sync_sites(
			FakeFCClient(
				{"first.frappe.cloud": {"host_name": "shared.example.com", "apps": ["access_guard"]}}
			)
		)

		# A different FC site now reports the same host. The claimed row must not
		# be re-pointed; this surfaces as a failure (host is unique) rather than
		# silently reassigning another site's registration.
		summary = fc.sync_sites(
			FakeFCClient(
				{"second.frappe.cloud": {"host_name": "shared.example.com", "apps": ["access_guard"]}}
			)
		)

		self.assertEqual(summary["failed"], 1)
		self.assertEqual(
			frappe.db.get_value("Remote Site", {"host": "shared.example.com"}, "fc_site_id"),
			"first.frappe.cloud",
			"the original claim stands",
		)


class TestTokenReconciliation(FCTestCase):
	"""A site enrolled against THIS hub already holds a transport token. Whether
	the local row's hash still matches it decides between "already enrolled" and
	"must re-enrol" — the hub can never recover the token's plaintext, so those
	are the only two honest outcomes."""

	def our_key(self):
		from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL

		return frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)

	def test_matching_token_marks_enrolled_and_skips_rotation(self):
		"""Hub restored from backup: the row and the site agree, so enrollment
		must not rotate a working token."""
		self.enable_fc()
		self.cleanup_fc_site("backup.frappe.cloud")
		manual = self.make_remote_site("erp.backup.com", transport_token="known-deployed-token-xyz")
		original_hash = manual.transport_token_hash

		client = FakeFCClient(
			{
				"backup.frappe.cloud": {
					"host_name": "erp.backup.com",
					"apps": ["access_guard"],
					"hub_key": self.our_key(),
					"token_hash": original_hash,
				}
			}
		)
		summary = fc.sync_sites(client)

		self.assertEqual(summary["token_matched"], 1)
		manual.reload()
		self.assertTrue(manual.fc_enrolled_at, "a verified token means already enrolled")
		self.assertEqual(manual.transport_token_hash, original_hash)

		# ...and enrollment leaves it alone entirely.
		fc.enrol_pending_sites(client)
		self.assertEqual(
			client.writes_for("backup.frappe.cloud"), [], "must not re-push config to a verified site"
		)
		manual.reload()
		self.assertEqual(manual.transport_token_hash, original_hash, "token must not rotate")

	def test_mismatched_token_forces_reenrollment(self):
		"""Same hub, but the site holds a token we can't authenticate — the guard
		would fail every ingest call. Re-push rather than leave it broken."""
		self.enable_fc()
		self.cleanup_fc_site("drifted.frappe.cloud")
		manual = self.make_remote_site("erp.drifted.com", transport_token="local-token-aaa")
		local_hash = manual.transport_token_hash

		client = FakeFCClient(
			{
				"drifted.frappe.cloud": {
					"host_name": "erp.drifted.com",
					"apps": ["access_guard"],
					"hub_key": self.our_key(),
					"token_hash": "0" * 64,  # some other token entirely
				}
			}
		)
		summary = fc.sync_sites(client)

		self.assertEqual(summary["token_matched"], 0)
		manual.reload()
		self.assertFalse(manual.fc_enrolled_at, "unverifiable token must not count as enrolled")
		self.assertEqual(manual.transport_token_hash, local_hash, "discovery doesn't rotate; enrollment does")

		fc.enrol_pending_sites(client)
		writes = client.writes_for("drifted.frappe.cloud")
		self.assertEqual(len(writes), 1, "must re-push a known token")
		manual.reload()
		self.assertEqual(manual.transport_token_hash, writes[0]["token_hash"])
		self.assertNotEqual(manual.transport_token_hash, local_hash, "rotated to the pushed token")

	def test_our_hub_key_but_no_token_is_reenrolled(self):
		"""Partially configured: hub key present, token missing."""
		self.enable_fc()
		self.cleanup_fc_site("partialcfg.frappe.cloud")
		client = FakeFCClient(
			{
				"partialcfg.frappe.cloud": {
					"host_name": "erp.partialcfg.com",
					"apps": ["access_guard"],
					"hub_key": self.our_key(),
					"token_hash": None,
				}
			}
		)

		summary = fc.sync_sites(client)

		self.assertEqual(summary["token_matched"], 0)
		site = frappe.get_doc("Remote Site", {"fc_site_id": "partialcfg.frappe.cloud"})
		self.assertFalse(site.fc_enrolled_at)

	def test_new_site_never_claims_a_token_it_cannot_produce(self):
		"""A fresh row autogenerates a hash; if the site already holds a different
		token, the row must not be stamped enrolled off the back of that."""
		self.enable_fc()
		self.cleanup_fc_site("freshbutcfg.frappe.cloud")
		client = FakeFCClient(
			{
				"freshbutcfg.frappe.cloud": {
					"host_name": "erp.freshbutcfg.com",
					"apps": ["access_guard"],
					"hub_key": self.our_key(),
					"token_hash": "f" * 64,
				}
			}
		)

		summary = fc.sync_sites(client)

		self.assertEqual(summary["created"], 1)
		self.assertEqual(summary["token_matched"], 0)
		site = frappe.get_doc("Remote Site", {"fc_site_id": "freshbutcfg.frappe.cloud"})
		self.assertFalse(site.fc_enrolled_at, "unknown deployed token = not enrolled")
		self.assertNotEqual(site.transport_token_hash, "f" * 64, "must not adopt a hash we can't rotate")

	def test_verified_token_does_not_reenable_a_disabled_site(self):
		"""Proving the token works is not a reason to override the kill switch."""
		self.enable_fc()
		self.cleanup_fc_site("verifiedoff.frappe.cloud")
		manual = self.make_remote_site(
			"erp.verifiedoff.com", transport_token="still-good-token-123", access_disabled=1
		)

		fc.sync_sites(
			FakeFCClient(
				{
					"verifiedoff.frappe.cloud": {
						"host_name": "erp.verifiedoff.com",
						"apps": ["access_guard"],
						"hub_key": self.our_key(),
						"token_hash": manual.transport_token_hash,
					}
				}
			)
		)

		manual.reload()
		self.assertTrue(manual.fc_enrolled_at)
		self.assertTrue(manual.access_disabled, "kill switch survives token verification")

	def test_config_deleted_from_site_is_repaired(self):
		"""Someone wiped the guard config off an enrolled site. The stale
		fc_enrolled_at stamp must be cleared, or enrollment (which filters on it)
		would skip the site forever and it would just stop working."""
		self.enable_fc()
		self.cleanup_fc_site("wiped.frappe.cloud")
		manual = self.make_remote_site("erp.wiped.com", transport_token="was-deployed-token-1")
		site_cfg = {
			"host_name": "erp.wiped.com",
			"apps": ["access_guard"],
			"hub_key": self.our_key(),
			"token_hash": manual.transport_token_hash,
		}
		client = FakeFCClient({"wiped.frappe.cloud": site_cfg})

		fc.sync_sites(client)
		manual.reload()
		self.assertTrue(manual.fc_enrolled_at, "verified, so stamped")

		# Config wiped off the site.
		site_cfg["hub_key"] = None
		site_cfg["token_hash"] = None

		summary = fc.sync_sites(client)
		self.assertEqual(summary["token_matched"], 0)
		manual.reload()
		self.assertFalse(manual.fc_enrolled_at, "stale stamp must be cleared so it can be repaired")

		# ...and the next enrollment pass pushes config back.
		fc.enrol_pending_sites(client)
		writes = client.writes_for("wiped.frappe.cloud")
		self.assertEqual(len(writes), 1, "config is pushed back")
		manual.reload()
		self.assertEqual(manual.transport_token_hash, writes[0]["token_hash"])

	def test_token_replaced_on_site_is_repaired(self):
		"""Same shape, but the config was overwritten rather than deleted (e.g.
		someone pasted another hub's token)."""
		self.enable_fc()
		self.cleanup_fc_site("overwritten.frappe.cloud")
		manual = self.make_remote_site("erp.overwritten.com", transport_token="original-token-9")
		site_cfg = {
			"host_name": "erp.overwritten.com",
			"apps": ["access_guard"],
			"hub_key": self.our_key(),
			"token_hash": manual.transport_token_hash,
		}
		client = FakeFCClient({"overwritten.frappe.cloud": site_cfg})
		fc.sync_sites(client)
		manual.reload()
		self.assertTrue(manual.fc_enrolled_at)

		site_cfg["token_hash"] = "9" * 64  # replaced out from under us
		fc.sync_sites(client)

		manual.reload()
		self.assertFalse(manual.fc_enrolled_at, "an unknown token is not an enrolment")

	def test_no_repair_when_auto_enroll_is_off(self):
		"""fc_auto_enroll_sites off is how an operator says "don't touch this
		site's config" - so a wiped config is left alone, stamp and all."""
		self.enable_fc()
		self.cleanup_fc_site("norepair.frappe.cloud")
		manual = self.make_remote_site("erp.norepair.com", transport_token="leave-me-alone-tok")
		site_cfg = {
			"host_name": "erp.norepair.com",
			"apps": ["access_guard"],
			"hub_key": self.our_key(),
			"token_hash": manual.transport_token_hash,
		}
		client = FakeFCClient({"norepair.frappe.cloud": site_cfg})
		fc.sync_sites(client)
		manual.reload()
		stamped_at = manual.fc_enrolled_at
		self.assertTrue(stamped_at)

		# Config wiped, but auto-enroll is now off.
		site_cfg["hub_key"] = None
		site_cfg["token_hash"] = None
		self.enable_fc(fc_auto_enroll_sites=0)

		fc.sync_sites(client)
		manual.reload()
		self.assertEqual(manual.fc_enrolled_at, stamped_at, "stamp untouched while auto-enroll is off")

		fc.enrol_pending_sites(client)
		self.assertEqual(client.writes_for("norepair.frappe.cloud"), [], "and nothing is pushed")


class TestEnrolledSiteIsNeverReenabled(FCTestCase):
	"""The kill switch on an already-enrolled site must survive every pass."""

	def test_disabled_after_enrollment_stays_disabled(self):
		self.enable_fc()
		self.cleanup_fc_site("killafter.frappe.cloud")
		client = FakeFCClient(
			{"killafter.frappe.cloud": {"host_name": "erp.killafter.com", "apps": ["access_guard"]}}
		)
		created = fc.sync_sites(client)["created_names"]
		fc.enrol_pending_sites(client, newly_created=created)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "killafter.frappe.cloud"})
		self.assertFalse(site.access_disabled, "enrollment enabled it")
		stamped_at = site.fc_enrolled_at

		site.access_disabled = 1  # operator kills it
		site.save(ignore_permissions=True)

		# Full pass, twice, both halves.
		fc.run_sync()
		fc.run_sync()

		site.reload()
		self.assertTrue(site.access_disabled, "an enrolled site is never re-enabled")
		self.assertEqual(site.fc_enrolled_at, stamped_at)
		self.assertEqual(len(client.writes_for("killafter.frappe.cloud")), 1, "and no config is re-pushed")

	def test_repair_does_not_reenable_a_disabled_site(self):
		"""Config wiped off a site an operator had disabled: repair re-pushes the
		config, but must not switch access back on."""
		self.enable_fc()
		self.cleanup_fc_site("repairoff.frappe.cloud")
		site_cfg = {"host_name": "erp.repairoff.com", "apps": ["access_guard"]}
		client = FakeFCClient({"repairoff.frappe.cloud": site_cfg})
		fc.sync_sites(client)
		fc.enrol_pending_sites(client)

		site = frappe.get_doc("Remote Site", {"fc_site_id": "repairoff.frappe.cloud"})
		site.access_disabled = 1  # operator kills it
		site.save(ignore_permissions=True)

		# Config wiped off the site, then a full pass repairs it.
		site_cfg["hub_key"] = None
		site_cfg["token_hash"] = None
		fc.sync_sites(client)
		site.reload()
		self.assertFalse(site.fc_enrolled_at, "repair clears the stamp")

		fc.enrol_pending_sites(client)
		site.reload()
		self.assertTrue(site.fc_enrolled_at, "and re-pushes config")
		self.assertTrue(site.access_disabled, "but must not re-enable it")

	def test_adopted_site_is_not_enabled_by_enrollment(self):
		"""A hand-made row that was disabled stays disabled through enrollment."""
		self.enable_fc()
		self.cleanup_fc_site("adoptoff.frappe.cloud")
		manual = self.make_remote_site("erp.adoptoff.com", access_disabled=1)
		client = FakeFCClient(
			{"adoptoff.frappe.cloud": {"host_name": "erp.adoptoff.com", "apps": ["access_guard"]}}
		)

		fc.sync_sites(client)
		fc.enrol_pending_sites(client)

		manual.reload()
		self.assertEqual(manual.fc_site_id, "adoptoff.frappe.cloud", "adopted")
		self.assertTrue(manual.fc_enrolled_at, "config pushed")
		self.assertTrue(manual.access_disabled, "adoption is not consent to enable")


class TestDepartedSites(FCTestCase):
	"""is_fc_site is read_only, so the sync clearing it is the only way a row that
	leaves FC stops being treated as an FC site."""

	def test_site_no_longer_listed_is_released(self):
		self.enable_fc()
		self.cleanup_fc_site("leaving.frappe.cloud", host="erp.leaving.com")
		client = FakeFCClient(
			{
				"leaving.frappe.cloud": {"host_name": "erp.leaving.com", "apps": ["access_guard"]},
				"staying.frappe.cloud": {"host_name": "erp.staying.com", "apps": ["access_guard"]},
			}
		)
		self.cleanup_fc_site("staying.frappe.cloud", host="erp.staying.com")
		fc.sync_sites(client)
		leaving = frappe.get_doc("Remote Site", {"fc_site_id": "leaving.frappe.cloud"})

		del client.sites["leaving.frappe.cloud"]
		summary = fc.sync_sites(client)

		self.assertEqual(summary["departed"], 1)
		leaving.reload()
		self.assertFalse(leaving.is_fc_site, "the flag is cleared - nothing else can un-tick it")
		self.assertIsNone(leaving.fc_site_id)
		self.assertIsNone(leaving.fc_team)
		self.assertIsNone(leaving.fc_enrolled_at)

		# The row itself survives: grants and log files reference it.
		self.assertTrue(frappe.db.exists("Remote Site", leaving.name))
		self.assertTrue(
			frappe.db.exists("Remote Site", {"fc_site_id": "staying.frappe.cloud"}), "others untouched"
		)

	def test_release_keeps_access_state(self):
		"""Leaving FC is not a reason to change whether access is allowed."""
		self.enable_fc()
		self.cleanup_fc_site("goneenabled.frappe.cloud", host="erp.goneenabled.com")
		client = FakeFCClient(
			{"goneenabled.frappe.cloud": {"host_name": "erp.goneenabled.com", "apps": ["access_guard"]}}
		)
		created = fc.sync_sites(client)["created_names"]
		fc.enrol_pending_sites(client, newly_created=created)
		site = frappe.get_doc("Remote Site", {"fc_site_id": "goneenabled.frappe.cloud"})
		self.assertFalse(site.access_disabled)

		client.sites.clear()
		fc.sync_sites(client)

		site.reload()
		self.assertFalse(site.access_disabled, "access state is the operator's, not FC's")

	def test_empty_listing_releases_nothing(self):
		"""A bad/empty response must not read as "every site left FC"."""
		self.enable_fc()
		self.cleanup_fc_site("keepme.frappe.cloud", host="erp.keepme.com")
		client = FakeFCClient(
			{"keepme.frappe.cloud": {"host_name": "erp.keepme.com", "apps": ["access_guard"]}}
		)
		fc.sync_sites(client)

		summary = fc.sync_sites(FakeFCClient({}))

		self.assertEqual(summary["departed"], 0)
		self.assertTrue(
			frappe.db.exists("Remote Site", {"fc_site_id": "keepme.frappe.cloud"}), "must survive"
		)

	def test_other_hub_site_does_not_veto_releasing(self):
		"""A site belonging to another hub is still present on FC, so it is not
		departed - and its presence must not stop a genuinely departed row from
		being released."""
		self.enable_fc()
		self.cleanup_fc_site("mine.frappe.cloud", host="erp.mine.com")
		self.cleanup_fc_site("leaves.frappe.cloud", host="erp.leaves.com")
		client = FakeFCClient(
			{
				"mine.frappe.cloud": {"host_name": "erp.mine.com", "apps": ["access_guard"]},
				"leaves.frappe.cloud": {"host_name": "erp.leaves.com", "apps": ["access_guard"]},
			}
		)
		fc.sync_sites(client)

		# One site drops off FC; another appears that belongs to a different hub.
		del client.sites["leaves.frappe.cloud"]
		client.sites["theirs.frappe.cloud"] = {
			"host_name": "erp.theirs.com",
			"apps": ["access_guard"],
			"hub_key": "k4.public.NOT_OURS",
		}
		summary = fc.sync_sites(client)

		self.assertEqual(summary["other_hub"], 1)
		self.assertEqual(summary["departed"], 1, "the departed site is still released")
		self.assertTrue(
			frappe.db.exists("Remote Site", {"fc_site_id": "mine.frappe.cloud"}), "ours is untouched"
		)
		self.assertFalse(
			frappe.db.exists("Remote Site", {"fc_site_id": "leaves.frappe.cloud"}), "departed released"
		)

	def _fc_site(self, site_id="rot.frappe.cloud"):
		self.cleanup_fc_site(site_id, host=f"erp.{site_id}")
		client = FakeFCClient({site_id: {"host_name": f"erp.{site_id}", "apps": ["access_guard"]}})
		fc.sync_sites(client)
		return frappe.get_doc("Remote Site", {"fc_site_id": site_id}), client

	def test_rotation_pushes_to_fc_and_returns_no_config(self):
		from access_hub.access_hub.doctype.remote_site.remote_site import rotate_transport_token

		self.enable_fc()
		site, client = self._fc_site()
		old_hash = site.transport_token_hash

		with patch.object(fc, "FrappeCloudClient", return_value=client):
			result = rotate_transport_token(site.name)

		self.assertTrue(result.get("pushed_to_fc"))
		self.assertNotIn("config", result, "nothing for the operator to copy")
		writes = client.writes_for("rot.frappe.cloud")
		self.assertEqual(len(writes), 1)

		site.reload()
		self.assertNotEqual(site.transport_token_hash, old_hash, "rotated")
		self.assertEqual(site.transport_token_hash, writes[0]["token_hash"], "and pushed that token")
		self.assertTrue(site.fc_enrolled_at)

	def test_failed_push_does_not_store_the_new_token(self):
		"""If press refuses, the hub must not be left expecting a token the site
		never received - the old one keeps working."""
		from access_hub.access_hub.doctype.remote_site.remote_site import rotate_transport_token

		self.enable_fc()
		site, client = self._fc_site("rotfail.frappe.cloud")
		old_hash = site.transport_token_hash

		class Exploding(FakeFCClient):
			def update_config(self, site_id, config):
				raise RuntimeError("press said no")

		with patch.object(fc, "FrappeCloudClient", return_value=Exploding(client.sites)):
			with self.assertRaises(RuntimeError):
				rotate_transport_token(site.name)

		site.reload()
		self.assertEqual(site.transport_token_hash, old_hash, "old token still authoritative")

	def test_non_fc_site_still_returns_config_to_copy(self):
		from access_hub.access_hub.doctype.remote_site.remote_site import rotate_transport_token

		self.enable_fc()
		manual = self.make_remote_site(f"rot-manual-{self._testMethodName}.localhost")

		result = rotate_transport_token(manual.name)

		self.assertFalse(result.get("pushed_to_fc"))
		self.assertIn("access_guard_transport_token", result["config"])

	def test_no_push_when_auto_enroll_is_off(self):
		"""Auto-enrol off means the hub doesn't touch client config - so the
		operator gets the config to apply by hand, as before."""
		from access_hub.access_hub.doctype.remote_site.remote_site import rotate_transport_token

		self.enable_fc()
		site, client = self._fc_site("rotmanual.frappe.cloud")
		self.enable_fc(fc_auto_enroll_sites=0)

		with patch.object(fc, "FrappeCloudClient", return_value=client):
			result = rotate_transport_token(site.name)

		self.assertFalse(result.get("pushed_to_fc"))
		self.assertIn("config", result)
		self.assertEqual(len(client.writes_for("rotmanual.frappe.cloud")), 0)

	def test_unpushable_fc_site_still_surfaces_the_config(self):
		"""The bug this replaced: gating the copy dialog on is_fc_site meant an FC
		site we can't push to (auto-enrol off) got neither a push nor a dialog, so
		its token was unrecoverable."""
		self.enable_fc()
		site, _ = self._fc_site("nodialog.frappe.cloud")
		self.enable_fc(fc_auto_enroll_sites=0)

		site.rotate_and_build_config()
		self.assertTrue(
			site.flags.get("new_token_plaintext"),
			"undelivered token must stay flagged so onload can show it",
		)
		site.onload()
		self.assertIn("transport_token_config", site.get_onload())

	def test_pushed_fc_site_shows_no_config(self):
		self.enable_fc()
		site, client = self._fc_site("dialogless.frappe.cloud")

		config = site.rotate_and_build_config()
		fc.push_guard_config(site, config, client)

		self.assertIsNone(site.flags.get("new_token_plaintext"), "delivered, so nothing to copy")
		site.onload()
		self.assertNotIn("transport_token_config", site.get_onload() or {})

	def test_site_that_uninstalled_the_guard_is_released(self):
		"""seen_fc_ids is recorded after the access_guard check, so a site that
		removes the app stops being an FC site here - correct, since without the
		guard this hub can't serve it."""
		self.enable_fc()
		self.cleanup_fc_site("unguarded.frappe.cloud", host="erp.unguarded.com")
		site_cfg = {"host_name": "erp.unguarded.com", "apps": ["access_guard", "frappe"]}
		client = FakeFCClient({"unguarded.frappe.cloud": site_cfg})
		fc.sync_sites(client)
		site = frappe.get_doc("Remote Site", {"fc_site_id": "unguarded.frappe.cloud"})

		site_cfg["apps"] = ["frappe"]  # guard uninstalled
		summary = fc.sync_sites(client)

		self.assertEqual(summary["with_guard"], 0)
		self.assertEqual(summary["departed"], 1)
		site.reload()
		self.assertFalse(site.is_fc_site)
		self.assertIsNone(site.fc_site_id)

	def test_a_failed_site_suppresses_the_whole_release(self):
		"""A transient error must never look like sites departing."""
		self.enable_fc()
		self.cleanup_fc_site("kept.frappe.cloud", host="erp.kept.com")
		client = FakeFCClient({"kept.frappe.cloud": {"host_name": "erp.kept.com", "apps": ["access_guard"]}})
		fc.sync_sites(client)

		class Exploding(FakeFCClient):
			def installed_apps(self, site_id):
				raise RuntimeError("press blipped")

		summary = fc.sync_sites(Exploding(client.sites))

		self.assertEqual(summary["failed"], 1)
		self.assertEqual(summary["departed"], 0, "no release on a sweep that errored")
		self.assertTrue(frappe.db.exists("Remote Site", {"fc_site_id": "kept.frappe.cloud"}))
