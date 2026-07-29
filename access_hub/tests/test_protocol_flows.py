# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import json
import os

import frappe
import pyseto
from frappe.tests.utils import FrappeTestCase
from pyseto import Key

from access_hub.access_hub.doctype.access_grant.access_grant import create_and_mint
from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
	get_current_policy_token,
	push_policy_to_sites,
)
from access_hub.access_hub.doctype.session_log_file.session_log_file import (
	append_lines,
	base_dir,
	path_for,
)
from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL, POLICY_TOKEN_CACHE_KEY, TRANSPORT_TOKEN_MIN_LEN
from access_hub.protocol import (
	POLICY_ACCESS_DISABLED,
	POLICY_RULES,
	PV,
	TRANSPORT_TOKEN_HEADER,
	transport_token_hash,
)


def get_public_key() -> Key:
	public_paserk = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
	return Key.from_paserk(public_paserk)


def decode_claims(token: str) -> dict:
	return json.loads(pyseto.decode(get_public_key(), token).payload)


class HubTestCase(FrappeTestCase):
	"""Common fixture helpers.

	FrappeTestCase's own DB rollback only happens once, at class teardown
	(`addClassCleanup(_rollback_db)` in setUpClass) — NOT per test method.
	That rollback also cannot undo anything already committed mid-test: a
	real HTTP request through frappe's WSGI app auto-commits at the end of a
	successful request (frappe/app.py's request teardown calls
	frappe.db.commit()), and a couple of tests call frappe.db.commit()
	directly for the same reason (making a row visible to a second DB
	connection opened by that HTTP request). Anything committed - or written
	to the real filesystem, which no DB rollback touches at all - must be
	cleaned up explicitly or repeat `bench run-tests` runs against the same
	bench collide with leftover rows/files from the previous run.

	Register cleanup with `self.addCleanup(...)` right where each fixture is
	created (LIFO per-test, not a single tearDown that has to separately
	track every test method's fixtures).

	Same reasoning applies to redis: get_current_policy_token() caches the
	signed token there, and DB rollback doesn't touch it - clear it up front so
	no test can read a token signed against a since-rolled-back state.
	"""

	def setUp(self):
		# One key per site (policy_token_cache_key) - wildcard, since the sites a
		# previous test cached under are already gone by now.
		frappe.cache.delete_keys(f"{POLICY_TOKEN_CACHE_KEY}:*")

	def make_remote_site(self, host, transport_token=None, **fields):
		site = frappe.get_doc(
			{
				"doctype": "Remote Site",
				"host": host,
				"port": 8005,
				"protocol": "http",
				**fields,
			}
		)
		# Set a known plaintext when the test needs one; otherwise validate()
		# autogenerates. Either way only the hash is stored.
		if transport_token is not None:
			site._apply_token(transport_token)
		site.insert(ignore_permissions=True)
		# name is a hash now (autoname), not the host — clean up by name.
		self.addCleanup(self._delete_remote_site, site.name)
		return site

	@staticmethod
	def _delete_remote_site(name):
		frappe.delete_doc("Remote Site", name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def make_user(self, email, **fields):
		if frappe.db.exists("User", email):
			return frappe.get_doc("User", email)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				**fields,
			}
		)
		user.insert(ignore_permissions=True)
		self.addCleanup(self._delete_user, email)
		return user

	@staticmethod
	def _delete_user(email):
		frappe.delete_doc("User", email, ignore_permissions=True, force=True)
		frappe.db.commit()

	def register_log_file_cleanup(self, remote_site, jti):
		"""append_lines() writes real files under base_dir() - no DB rollback
		touches those. Delete both on-disk files (request + doc kind), plus
		any Session Log File rows a real ingest call may have committed."""
		self.addCleanup(self._delete_log_artifacts, remote_site, jti)

	@staticmethod
	def _delete_log_artifacts(remote_site, jti):
		for log_kind in ("request", "doc"):
			p = path_for(remote_site, jti, log_kind)
			if os.path.exists(p):
				os.remove(p)
		frappe.db.delete("Session Log File", {"jti": jti})
		frappe.db.commit()


class TestKeypairAndGrantMinting(HubTestCase):
	def test_mint_and_verify_grant_token(self):
		"""keypair gen -> mint token -> verify with the public key -> claims
		match; aud == transport_token_hash."""
		site = self.make_remote_site("test-grant-site.localhost")
		user = self.make_user("grant-employee@example.com")

		token = create_and_mint(user.name, site)
		claims = decode_claims(token)

		# Access Grant is autonamed by jti (see test_grant_autonames_by_jti).
		grant = frappe.get_doc("Access Grant", claims["jti"])
		self.assertEqual(grant.status, "Issued")
		self.assertEqual(grant.user_email, user.email or user.name)

		self.assertEqual(claims["user_email"], user.email or user.name)
		self.assertEqual(claims["pv"], PV)

		site.reload()
		self.assertEqual(claims["aud"], site.transport_token_hash)

		url = site.build_accept_url(token)
		self.assertIn(token, url)
		self.assertIn("access_guard.api.accept", url)

	def test_grant_token_tamper_fails_verification(self):
		site = self.make_remote_site("test-tamper-site.localhost")
		user = self.make_user("tamper-employee@example.com")
		token = create_and_mint(user.name, site)

		tampered = token[:-4] + ("A" * 4 if not token.endswith("AAAA") else "BBBB")
		pub = get_public_key()
		with self.assertRaises(Exception):
			pyseto.decode(pub, tampered)

	def test_grant_autonames_by_jti(self):
		"""Access Grant now folds in what Session Log used to be
		responsible for; autoname must be the jti itself so a lookup is a
		direct frappe.get_doc("Access Grant", jti), no filter needed."""
		site = self.make_remote_site("test-autoname-site.localhost")
		user = self.make_user("autoname-employee@example.com")
		token = create_and_mint(user.name, site)
		grant = frappe.get_doc("Access Grant", decode_claims(token)["jti"])
		self.assertEqual(grant.name, grant.jti)


class TestSessionLifecycleEvents(HubTestCase):
	"""record_session_event / report_session_event: the merged Access Grant
	tracks session start/end + logout reason, pushed immediately by the
	guard (not batched with the hourly activity-log sync)."""

	def setUp(self):
		super().setUp()
		self.site = self.make_remote_site(f"test-session-event-{self._testMethodName}.localhost")
		self.user = self.make_user(f"session-event-{self._testMethodName}@example.com")
		self.jti = decode_claims(create_and_mint(self.user.name, self.site))["jti"]

	def test_login_sets_active_and_start_time(self):
		from access_hub.access_hub.doctype.access_grant.access_grant import record_session_event

		record_session_event(self.site.name, self.jti, "login")
		grant = frappe.get_doc("Access Grant", self.jti)
		self.assertEqual(grant.status, "Active")
		self.assertTrue(grant.start_time)

		first_start_time = grant.start_time
		record_session_event(self.site.name, self.jti, "login")
		grant.reload()
		self.assertEqual(grant.status, "Active")
		self.assertEqual(grant.start_time, first_start_time, "start_time must not move on a repeated event")

	def test_logout_sets_ended_end_time_and_reason(self):
		from access_hub.access_hub.doctype.access_grant.access_grant import record_session_event

		record_session_event(self.site.name, self.jti, "login")
		record_session_event(self.site.name, self.jti, "logout", reason="idle_expired")

		grant = frappe.get_doc("Access Grant", self.jti)
		self.assertEqual(grant.status, "Ended")
		self.assertTrue(grant.end_time)
		self.assertEqual(grant.logout_reason, "idle_expired")

	def test_unknown_jti_is_a_no_op(self):
		from access_hub.access_hub.doctype.access_grant.access_grant import record_session_event

		record_session_event(self.site.name, "no-such-jti-at-all", "login")  # must not raise

	def test_event_for_another_sites_grant_is_a_no_op(self):
		"""Authentication says which site is calling; it must not also grant
		authority over another site's records. A grant belongs to exactly one
		Remote Site, so an event reported by a *different* site is ignored -
		otherwise any enrolled client could forge another's audit trail."""
		from access_hub.access_hub.doctype.access_grant.access_grant import record_session_event

		other = self.make_remote_site(f"other-site-{self._testMethodName}.localhost")
		record_session_event(other.name, self.jti, "logout", reason="FORGED")

		grant = frappe.get_doc("Access Grant", self.jti)
		self.assertEqual(grant.status, "Issued", "another site's event must not change status")
		self.assertIsNone(grant.logout_reason)


class TestPolicyToken(HubTestCase):
	def test_policy_token_round_trips_and_seeds_defaults(self):
		"""policy token signs + round-trips; DEFAULT_RULES seed present."""
		settings = frappe.get_single("Access Hub Settings")
		if not settings.rules:
			from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
				seed_default_policy,
			)

			seed_default_policy()
			settings = frappe.get_single("Access Hub Settings")

		site = self.make_remote_site(f"test-policy-{self._testMethodName}.localhost")
		token = get_current_policy_token(site)
		pub = get_public_key()
		decoded = pyseto.decode(pub, token)
		claims = json.loads(decoded.payload)

		self.assertEqual(claims["pv"], PV)
		self.assertIn(POLICY_RULES, claims)

		self.assertTrue(any(r["doctype"] == "User" and r["mode"] == "deny" for r in claims[POLICY_RULES]))

	def test_blacklisted_users_and_access_disabled_round_trip_into_policy_dict(self):
		from access_hub.protocol import POLICY_BLACKLISTED_EMAILS

		settings = frappe.get_single("Access Hub Settings")
		if not settings.rules:
			from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
				seed_default_policy,
			)

			seed_default_policy()
			settings = frappe.get_single("Access Hub Settings")
		orig_disabled = settings.access_disabled
		orig_blacklist = [row.user for row in settings.blacklisted_users]

		def restore():
			# Runs after make_user's own cleanup (LIFO) has already deleted the
			# test user, so drop any blacklist row whose User no longer exists
			# rather than re-adding a now-dangling Link. Commit explicitly:
			# make_user's own cleanup already forced a commit, taking this
			# Single's state outside the class-level rollback's reach, so this
			# restore must also commit or a leftover row survives to the next run.
			s = frappe.get_single("Access Hub Settings")
			s.access_disabled = orig_disabled
			s.set("blacklisted_users", [{"user": u} for u in orig_blacklist if frappe.db.exists("User", u)])
			s.save(ignore_permissions=True)
			frappe.db.commit()

		self.addCleanup(restore)

		user = self.make_user(f"policy-dict-{self._testMethodName}@example.com", first_name="Policy")
		settings.access_disabled = 1
		settings.append("blacklisted_users", {"user": user.name})
		settings.save(ignore_permissions=True)

		policy = settings.as_policy_dict()
		self.assertTrue(policy[POLICY_ACCESS_DISABLED])
		self.assertIn(user.name, policy[POLICY_BLACKLISTED_EMAILS])
		self.assertTrue(settings.is_blacklisted(user.name))

		# access_disabled alone (no blacklist) blocks everyone; isolate the
		# blacklist-only case by turning the global switch back off.
		settings.access_disabled = 0
		settings.save(ignore_permissions=True)
		self.assertTrue(settings.is_blacklisted(user.name), "still blacklisted individually")

		other_user = self.make_user(f"someone-else-{self._testMethodName}@example.com", first_name="Other")
		self.assertFalse(settings.is_blacklisted(other_user.name))


class TestDisabledAndDeletedUserEviction(HubTestCase):
	"""A User disabled or deleted after having been granted access must be
	evicted (unioned into blacklisted_emails) automatically, without ever
	touching the admin-edited Blacklisted Users table — and it must clear
	itself the moment the condition is no longer true, with no cleanup code."""

	def test_disabled_user_with_grant_is_evicted_and_clears_on_reenable(self):
		from access_hub.access_hub.doctype.access_grant.access_grant import create_and_mint
		from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
			disabled_granted_user_emails,
		)

		site = self.make_remote_site(f"test-evict-{self._testMethodName}.localhost")
		user = self.make_user(f"evict-disable-{self._testMethodName}@example.com", first_name="Evictee")
		create_and_mint(user.name, site)
		self.addCleanup(frappe.db.delete, "Access Grant", {"user": user.name})

		self.assertNotIn(user.name, disabled_granted_user_emails())

		user.enabled = 0
		user.save(ignore_permissions=True)
		self.assertIn(user.name, disabled_granted_user_emails())

		settings = frappe.get_single("Access Hub Settings")
		self.assertTrue(settings.is_blacklisted(user.name))

		user.enabled = 1
		user.save(ignore_permissions=True)
		self.assertNotIn(user.name, disabled_granted_user_emails())

	def test_deleted_user_with_grant_stays_evicted(self):
		"""user_email is snapshotted onto Access Grant at mint time, so eviction
		survives the User row being deleted outright (not just disabled)."""
		from access_hub.access_hub.doctype.access_grant.access_grant import create_and_mint
		from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
			disabled_granted_user_emails,
		)

		site = self.make_remote_site(f"test-evict-del-{self._testMethodName}.localhost")
		email = f"evict-delete-{self._testMethodName}@example.com"
		user = self.make_user(email, first_name="Evictee")
		create_and_mint(user.name, site)
		self.addCleanup(frappe.db.delete, "Access Grant", {"user_email": email})

		# force=True: mirrors a real delete of a User with linked grants.
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()

		self.assertIn(email, disabled_granted_user_emails())

	def test_user_disable_triggers_policy_republish(self):
		from unittest.mock import patch

		user = self.make_user(f"evict-push-{self._testMethodName}@example.com", first_name="PushTest")

		with patch("frappe.enqueue") as mock_enqueue:
			user.enabled = 0
			user.save(ignore_permissions=True)

		self.assertTrue(
			any(c.args and c.args[0] is push_policy_to_sites for c in mock_enqueue.call_args_list)
		)

	def test_user_delete_triggers_policy_republish(self):
		from unittest.mock import patch

		user = self.make_user(f"evict-push-del-{self._testMethodName}@example.com", first_name="PushTest")
		# make_user's own cleanup would try to delete this same user again;
		# harmless no-op via frappe.db.exists guard in _delete_user, but we
		# delete it ourselves here so the push-mock window is tight.

		with patch("frappe.enqueue") as mock_enqueue:
			frappe.delete_doc("User", user.name, force=True, ignore_permissions=True)
			frappe.db.commit()

		self.assertTrue(
			any(c.args and c.args[0] is push_policy_to_sites for c in mock_enqueue.call_args_list)
		)

	def test_user_reenable_triggers_policy_republish(self):
		from unittest.mock import patch

		user = self.make_user(
			f"evict-push-reenable-{self._testMethodName}@example.com", first_name="PushTest"
		)
		user.enabled = 0
		user.save(ignore_permissions=True)

		with patch("frappe.enqueue") as mock_enqueue:
			user.enabled = 1
			user.save(ignore_permissions=True)

		self.assertTrue(
			any(c.args and c.args[0] is push_policy_to_sites for c in mock_enqueue.call_args_list)
		)

	def test_new_user_creation_triggers_policy_republish(self):
		from unittest.mock import patch

		with patch("frappe.enqueue") as mock_enqueue:
			self.make_user(f"evict-push-new-{self._testMethodName}@example.com", first_name="PushTest")

		self.assertTrue(
			any(c.args and c.args[0] is push_policy_to_sites for c in mock_enqueue.call_args_list)
		)


class TestRemoteSite(HubTestCase):
	def test_transport_token_hash_set_on_validate(self):
		# Fresh doc autogenerates a token; only its hash is ever stored (there is
		# no plaintext field on the doctype at all).
		site = self.make_remote_site("test-hash-site.localhost")
		self.assertTrue(site.transport_token_hash)
		site.reload()
		self.assertTrue(site.transport_token_hash)
		# there is no plaintext field on the doctype at all
		self.assertIsNone(site.meta.get_field("transport_token"))

	def test_short_token_rejected(self):
		self.assertLess(len("short"), TRANSPORT_TOKEN_MIN_LEN)
		with self.assertRaises(frappe.ValidationError):
			self.make_remote_site("test-shorttoken-site.localhost", transport_token="short")

	def test_autogenerated_token_matches_hash(self):
		site = self.make_remote_site("test-autogen-site.localhost")
		self.assertTrue(site.transport_token_hash)


class TestIngestAuth(HubTestCase):
	def setUp(self):
		super().setUp()
		self.plaintext_token = "super-secret-transport-token-123"
		host = f"test-ingest-{self._testMethodName}.localhost"
		self.site = self.make_remote_site(host, transport_token=self.plaintext_token)

	def _call_with_bearer(self, fn, token, **kwargs):
		"""Simulate the transport-token header path by monkeypatching
		frappe.get_request_header for the duration of the call, since these
		are whitelisted functions we're calling directly (no live HTTP)."""
		original = frappe.get_request_header

		def fake_header(key, default=None):
			if key == TRANSPORT_TOKEN_HEADER and token is not None:
				return token
			return original(key, default)

		frappe.get_request_header = fake_header
		try:
			return fn(**kwargs)
		finally:
			frappe.get_request_header = original

	def test_correct_token_passes(self):
		from access_hub.api import get_policy

		result = self._call_with_bearer(get_policy, self.plaintext_token, pv=PV)
		self.assertIn("policy_token", result)

	def test_wrong_token_fails(self):
		from access_hub.api import get_policy
		from access_hub.protocol import TransportAuthError

		result = self._call_with_bearer(get_policy, "totally-wrong-token", pv=PV)
		self.assertEqual(result["error"]["code"], TransportAuthError.code)

	def test_pv_mismatch_returns_426(self):
		from access_hub.api import get_policy
		from access_hub.protocol import ProtocolVersionError

		result = self._call_with_bearer(get_policy, self.plaintext_token, pv=PV + 1)
		self.assertEqual(result["error"]["code"], ProtocolVersionError.code)
		self.assertEqual(frappe.local.response.get("http_status_code"), ProtocolVersionError.http_status_code)

	def test_transport_token_hash_matches(self):
		self.site.reload()
		self.assertEqual(self.site.transport_token_hash, transport_token_hash(self.plaintext_token))


class TestLogServing(HubTestCase):
	def setUp(self):
		super().setUp()
		host = f"test-logserve-{self._testMethodName}.localhost"
		self.site = self.make_remote_site(host)

	def test_download_serves_full_file_content(self):
		from access_hub.access_hub.doctype.session_log_file.session_log_file import upsert_index
		from access_hub.api import download_session_log

		jti = f"test-log-dl-{self._testMethodName}-jti"
		self.register_log_file_cleanup(self.site.name, jti)
		append_lines(self.site.name, jti, "request", "ts=1 path=x\nts=2 path=y")
		doc = upsert_index(self.site.name, jti, log_kind="request", added_lines=2)

		frappe.set_user("Administrator")
		download_session_log(name=doc.name)
		self.assertEqual(frappe.local.response.get("type"), "download")
		self.assertEqual(frappe.local.response.get("filename"), f"{jti}.log")
		self.assertIn(b"path=x", frappe.local.response.get("filecontent"))

	def test_download_serves_doc_kind_with_correct_extension(self):
		from access_hub.access_hub.doctype.session_log_file.session_log_file import upsert_index
		from access_hub.api import download_session_log

		jti = f"test-log-dl-doc-{self._testMethodName}-jti"
		self.register_log_file_cleanup(self.site.name, jti)
		append_lines(self.site.name, jti, "doc", "ts=1 event=write doctype=User")
		doc = upsert_index(self.site.name, jti, log_kind="doc", added_lines=1)

		frappe.set_user("Administrator")
		download_session_log(name=doc.name)
		self.assertEqual(frappe.local.response.get("filename"), f"{jti}.doc.log")

	def test_traversal_via_host_is_rejected(self):
		# os.path.basename() already neutralizes "../../etc" -> "etc" (stays
		# inside base_dir as a subdirectory) - the case that can still reach
		# outside base_dir is a literal ".." component, which basename passes
		# through unchanged. _safe_join's realpath-containment check must
		# catch that.
		with self.assertRaises(frappe.PermissionError):
			path_for("..", "..")

	def test_traversal_via_jti_is_rejected(self):
		# jti gets a literal ".log" suffix appended after basename(), which
		# defangs a literal ".." (it becomes the filename "....log", not a
		# parent-dir reference) - so the jti axis can't reach outside
		# base_dir even without _safe_join. Exercise a case where the ".."
		# is only defanged by the suffix, and confirm it resolves safely
		# inside base_dir rather than raising (there's nothing to reject).
		p = path_for(self.site.name, "..")
		self.assertTrue(p.startswith(base_dir()))
		self.assertEqual(os.path.basename(p), "...log")

	def test_traversal_attempt_is_neutralized_not_just_rejected(self):
		# Even when basename() reduces a traversal-looking value to something
		# that doesn't raise, it must resolve to a safe filename *inside*
		# base_dir, never actually escaping it.
		p = path_for(self.site.name, "../../../etc/passwd")
		self.assertTrue(p.startswith(base_dir()))
		self.assertEqual(os.path.basename(p), "passwd.log")

	def test_resolved_paths_stay_inside_base_dir(self):
		p = path_for(self.site.name, "some-jti")
		base = base_dir()
		self.assertTrue(p.startswith(base))


class TestRequestAccessAttribution(HubTestCase):
	"""request_access() must always mint for the calling session user - no
	user= override - so a grant is never attributable to anyone but the
	person who actually clicked the button (DESIGN.md's no-approval-step,
	full-attribution posture)."""

	def setUp(self):
		super().setUp()
		self.site = self.make_remote_site(f"test-reqacc-{self._testMethodName}.localhost")
		self.caller_email = f"caller-{self._testMethodName}@example.com"
		caller = self.make_user(self.caller_email, first_name="Caller")
		# Desk User gets read-only on Remote Site — the role that can request.
		caller.add_roles("Desk User")
		self.addCleanup(frappe.set_user, "Administrator")

	def test_request_access_has_no_user_override_param(self):
		import inspect

		from access_hub.api import request_access

		self.assertNotIn("user", inspect.signature(request_access).parameters)

	def test_request_access_mints_for_calling_user(self):
		from access_hub.api import request_access

		frappe.set_user(self.caller_email)
		result = request_access(remote_site=self.site.name)
		self.assertIn("redirect_url", result)

		jti = frappe.db.get_value(
			"Access Grant", {"remote_site": self.site.name}, "jti", order_by="creation desc"
		)
		grant = frappe.get_doc("Access Grant", {"jti": jti})
		self.assertEqual(grant.user, self.caller_email)
		self.addCleanup(frappe.delete_doc, "Access Grant", grant.name, True, None, True)


class TestAccessDisabledAndBlacklist(HubTestCase):
	"""access_disabled and a Blacklisted User row must both refuse
	request_access outright, not just fail silently downstream."""

	def setUp(self):
		super().setUp()
		self.site = self.make_remote_site(f"test-blacklist-{self._testMethodName}.localhost")
		self.caller_email = f"blockable-{self._testMethodName}@example.com"
		caller = self.make_user(self.caller_email, first_name="Blockable")
		caller.add_roles("Desk User")
		self.addCleanup(frappe.set_user, "Administrator")

		self.settings = frappe.get_single("Access Hub Settings")
		if not self.settings.rules:
			from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
				seed_default_policy,
			)

			seed_default_policy()
			self.settings = frappe.get_single("Access Hub Settings")
		self._orig_disabled = self.settings.access_disabled
		self._orig_blacklist = [row.user for row in self.settings.blacklisted_users]
		self.addCleanup(self._restore_settings)

	def _restore_settings(self):
		# Commit explicitly: make_user's cleanup already forced a commit (see
		# HubTestCase docstring), so this Single's state is outside the
		# class-level rollback's reach — a leftover row would otherwise
		# survive to the next test run.
		settings = frappe.get_single("Access Hub Settings")
		settings.access_disabled = self._orig_disabled
		settings.set(
			"blacklisted_users",
			[{"user": u} for u in self._orig_blacklist if frappe.db.exists("User", u)],
		)
		settings.save(ignore_permissions=True)
		frappe.db.commit()

	def test_access_disabled_refuses_request(self):
		from access_hub.api import request_access

		self.settings.access_disabled = 1
		self.settings.save(ignore_permissions=True)

		frappe.set_user(self.caller_email)
		with self.assertRaises(frappe.ValidationError):
			request_access(remote_site=self.site.name)

	def test_blacklisted_user_refuses_request(self):
		from access_hub.api import request_access

		self.settings.append("blacklisted_users", {"user": self.caller_email})
		self.settings.save(ignore_permissions=True)

		frappe.set_user(self.caller_email)
		with self.assertRaises(frappe.ValidationError):
			request_access(remote_site=self.site.name)

	def test_unaffected_user_can_still_request(self):
		from access_hub.api import request_access

		frappe.set_user(self.caller_email)
		result = request_access(remote_site=self.site.name)
		self.assertIn("redirect_url", result)

		jti = frappe.db.get_value(
			"Access Grant", {"remote_site": self.site.name}, "jti", order_by="creation desc"
		)
		self.addCleanup(frappe.delete_doc, "Access Grant", jti, True, None, True)


class TestPerSiteAccessDisabled(HubTestCase):
	"""A Remote Site's own access_disabled must refuse requests to that site and
	reach only that site's guard as policy.access_disabled — the hub-wide switch
	stays off throughout, so anything asserted here is site-scoped."""

	def setUp(self):
		super().setUp()
		self.disabled = self.make_remote_site(
			f"test-site-off-{self._testMethodName}.localhost", access_disabled=1
		)
		self.enabled = self.make_remote_site(f"test-site-on-{self._testMethodName}.localhost")
		self.settings = frappe.get_single("Access Hub Settings")

	def _policy_of(self, site) -> dict:
		return decode_claims(get_current_policy_token(site))

	def test_flag_reaches_only_its_own_sites_policy(self):
		self.assertTrue(self._policy_of(self.disabled)[POLICY_ACCESS_DISABLED])
		self.assertFalse(self._policy_of(self.enabled)[POLICY_ACCESS_DISABLED])

	def test_hub_wide_switch_disables_every_site(self):
		orig = self.settings.access_disabled
		self.addCleanup(self._restore_hub_switch, orig)
		self.settings.db_set("access_disabled", 1)

		# Cached per site, and the fixtures above may already have populated
		# both keys — bust them so this reads the hub switch, not a stale token.
		frappe.cache.delete_keys(f"{POLICY_TOKEN_CACHE_KEY}:*")
		self.assertTrue(self._policy_of(self.enabled)[POLICY_ACCESS_DISABLED])
		self.assertTrue(self._policy_of(self.disabled)[POLICY_ACCESS_DISABLED])

	def _restore_hub_switch(self, orig):
		frappe.get_single("Access Hub Settings").db_set("access_disabled", orig)
		frappe.db.commit()

	def test_request_access_refused_for_disabled_site(self):
		from access_hub.api import request_access

		email = f"site-off-caller-{self._testMethodName}@example.com"
		caller = self.make_user(email, first_name="SiteOff")
		caller.add_roles("Desk User")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(email)

		with self.assertRaises(frappe.ValidationError):
			request_access(remote_site=self.disabled.name)

		# Same caller, same moment, only the site differs.
		result = request_access(remote_site=self.enabled.name)
		self.assertIn("redirect_url", result)
		jti = frappe.db.get_value(
			"Access Grant", {"remote_site": self.enabled.name}, "jti", order_by="creation desc"
		)
		self.addCleanup(frappe.delete_doc, "Access Grant", jti, True, None, True)

	def test_toggling_the_flag_pushes_that_site_only(self):
		from unittest.mock import patch

		with patch("frappe.enqueue") as mock_enqueue:
			self.enabled.access_disabled = 1
			self.enabled.save(ignore_permissions=True)

		pushes = [c for c in mock_enqueue.call_args_list if c.args and c.args[0] is push_policy_to_sites]
		self.assertTrue(pushes, "flipping the flag must enqueue a policy push")
		tokens = pushes[-1].kwargs["tokens"]
		self.assertEqual(list(tokens), [self.enabled.name])
		self.assertTrue(decode_claims(tokens[self.enabled.name])[POLICY_ACCESS_DISABLED])

	def test_unrelated_field_edit_does_not_republish(self):
		from unittest.mock import patch

		with patch("frappe.enqueue") as mock_enqueue:
			self.enabled.is_fc_site = 1
			self.enabled.save(ignore_permissions=True)

		self.assertFalse(
			[c for c in mock_enqueue.call_args_list if c.args and c.args[0] is push_policy_to_sites]
		)


class TestIngestEndpointsOverRealHttp(HubTestCase):
	"""Regression coverage for a real bug that only reproduced over an actual
	HTTP request, never via direct Python calls: frappe.whitelist() runs every
	wrapped function through validate_argument_types() ->
	transform_parameter_types(), which crashes with AttributeError if the
	wrapped object's __annotations__ is non-empty but its inspect.signature()
	doesn't have matching named parameters (exactly the shape
	ingest_endpoint's `*args, **kwargs` wrapper had after a since-reverted
	attempt to also fix frappe's automatic type coercion). Calling the
	Python functions directly - as the other test classes in this file do -
	never exercises frappe.whitelist's request-dispatch wrapper at all, so it
	can't catch this class of bug. Drive real HTTP requests through frappe's
	actual WSGI app instead.

	Every test method here goes through a real HTTP request, which
	auto-commits (frappe/app.py's request teardown) - so every fixture here
	uses make_remote_site()/register_log_file_cleanup() (never a bare
	frappe.get_doc(...).insert()) to guarantee cleanup runs even though
	FrappeTestCase's class-level rollback can't undo these commits.
	"""

	def setUp(self):
		super().setUp()
		self.plaintext_token = "http-regression-transport-token-1"
		self.host = f"test-http-{self._testMethodName}.localhost"
		self.site = self.make_remote_site(self.host, transport_token=self.plaintext_token)
		# A real minted grant, not a synthetic jti: the ingest endpoints require
		# the jti to belong to the authenticated site, so a made-up one is now
		# (correctly) a 401 - see PROTOCOL.md §5 "Authentication is not
		# authorization".
		self.user = self.make_user(f"http-test-{self._testMethodName}@example.com")
		self.jti = decode_claims(create_and_mint(self.user.name, self.site))["jti"]
		# logs are keyed by Remote Site name (a hash), not host — clean up by name.
		self.register_log_file_cleanup(self.site.name, self.jti)
		# Make the Remote Site visible to the second DB connection the HTTP
		# request below will open.
		frappe.db.commit()

	def _client(self):
		import frappe.app
		from werkzeug.test import Client

		return Client(frappe.app.application)

	def _post(self, method, body: bytes, token=None):
		headers = {"Host": "hub.localhost", "Content-Type": "application/json"}
		if token is not None:
			headers[TRANSPORT_TOKEN_HEADER] = token
		return self._client().post(f"/api/method/{method}", headers=headers, data=body)

	def test_get_policy_over_http(self):
		resp = self._post("access_hub.api.get_policy", b'{"pv":1}', token=self.plaintext_token)
		self.assertEqual(resp.status_code, 200)
		self.assertIn("policy_token", resp.get_data(as_text=True))

	def test_push_logs_over_http_with_typed_params(self):
		body = b'{"pv":1,"jti":"' + self.jti.encode() + b'","log_kind":"request","lines":"ts=1 path=x"}'
		resp = self._post("access_hub.api.push_logs", body, token=self.plaintext_token)
		self.assertEqual(resp.status_code, 200)
		self.assertIn("received_lines", resp.get_data(as_text=True))

	def test_report_session_event_over_http_with_typed_params(self):
		body = b'{"pv":1,"jti":"' + self.jti.encode() + b'","event":"logout","reason":"user_logout"}'
		resp = self._post("access_hub.api.report_session_event", body, token=self.plaintext_token)
		self.assertEqual(resp.status_code, 200)

	def test_pv_mismatch_over_http_returns_426(self):
		resp = self._post("access_hub.api.get_policy", b'{"pv":2}', token=self.plaintext_token)
		self.assertEqual(resp.status_code, 426)

	def test_push_logs_for_another_sites_jti_is_rejected(self):
		"""A valid transport token authenticates its OWN site only. Pushing
		under a jti belonging to a different Remote Site must fail, or any
		enrolled client could file logs against another tenant's grant (and
		pick up that grant's user_email via upsert_index)."""
		other_site = self.make_remote_site(f"other-http-{self._testMethodName}.localhost")
		other_user = self.make_user(f"other-http-{self._testMethodName}@example.com")
		other_jti = decode_claims(create_and_mint(other_user.name, other_site))["jti"]
		self.register_log_file_cleanup(other_site.name, other_jti)
		frappe.db.commit()

		body = b'{"pv":1,"jti":"' + other_jti.encode() + b'","log_kind":"request","lines":"ts=1 forged"}'
		resp = self._post("access_hub.api.push_logs", body, token=self.plaintext_token)
		self.assertEqual(resp.status_code, 401)
		self.assertFalse(
			frappe.db.exists("Session Log File", {"jti": other_jti}),
			"no log row may be created for another site's jti",
		)


class TestEnrollmentConfig(HubTestCase):
	def test_get_enrollment_config_shape(self):
		from access_hub.protocol import (
			GUARD_CONF_HUB_PUBLIC_KEY,
			GUARD_CONF_HUB_URL,
			GUARD_CONF_TRANSPORT_TOKEN,
		)

		site = self.make_remote_site("test-enrollcfg.localhost")
		config = site.get_enrollment_config("plaintext-token-123")
		# keyed by the protocol-defined guard config keys, not literals
		self.assertIn(GUARD_CONF_HUB_URL, config)
		self.assertIn(GUARD_CONF_HUB_PUBLIC_KEY, config)
		self.assertEqual(config[GUARD_CONF_TRANSPORT_TOKEN], "plaintext-token-123")
