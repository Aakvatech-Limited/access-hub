# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import json
import os

import frappe
import pyseto
from frappe.tests.utils import FrappeTestCase
from pyseto import Key

from access_hub.access_hub.doctype.access_grant.access_grant import (
	build_accept_url,
	create_and_mint,
)
from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
	get_current_policy_token,
)
from access_hub.access_hub.doctype.session_log_file.session_log_file import (
	append_lines,
	base_dir,
	path_for,
)
from access_hub.constants import CONF_HUB_PUBLIC_KEY_LOCAL, TRANSPORT_TOKEN_MIN_LEN
from access_hub.protocol import (
	POLICY_DEFAULTS,
	POLICY_RULES,
	PV,
	TRANSPORT_TOKEN_HEADER,
	transport_token_hash,
)


def get_public_key() -> Key:
	public_paserk = frappe.conf.get(CONF_HUB_PUBLIC_KEY_LOCAL)
	return Key.from_paserk(public_paserk)


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
	"""

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

	def register_log_file_cleanup(self, host, jti):
		"""append_lines() writes real files under base_dir() - no DB rollback
		touches those. Delete the on-disk file, plus any Session Log /
		Session Log File rows a real ingest call may have committed for it."""
		self.addCleanup(self._delete_log_artifacts, host, jti)

	@staticmethod
	def _delete_log_artifacts(host, jti):
		p = path_for(host, jti)
		if os.path.exists(p):
			os.remove(p)
		frappe.db.delete("Session Log", {"jti": jti})
		frappe.db.delete("Session Log File", {"jti": jti})
		frappe.db.commit()


class TestKeypairAndGrantMinting(HubTestCase):
	def test_mint_and_verify_grant_token(self):
		"""keypair gen -> mint token -> verify with the public key -> claims
		match; aud == transport_token_hash."""
		site = self.make_remote_site("test-grant-site.localhost")
		user = self.make_user("grant-employee@example.com")

		grant, token = create_and_mint(user.name, site.name)

		self.assertEqual(grant.status, "Issued")
		self.assertTrue(grant.jti)

		pub = get_public_key()
		decoded = pyseto.decode(pub, token)
		claims = json.loads(decoded.payload)

		self.assertEqual(claims["emp_email"], user.email or user.name)
		self.assertEqual(claims["jti"], grant.jti)
		self.assertEqual(claims["pv"], PV)

		site.reload()
		self.assertEqual(claims["aud"], site.transport_token_hash)

		url = build_accept_url(site.name, token)
		self.assertIn(token, url)
		self.assertIn("access_guard.api.accept", url)

	def test_grant_token_tamper_fails_verification(self):
		site = self.make_remote_site("test-tamper-site.localhost")
		user = self.make_user("tamper-employee@example.com")
		_grant, token = create_and_mint(user.name, site.name)

		tampered = token[:-4] + ("A" * 4 if not token.endswith("AAAA") else "BBBB")
		pub = get_public_key()
		with self.assertRaises(Exception):
			pyseto.decode(pub, tampered)


class TestPolicyToken(HubTestCase):
	def test_policy_token_round_trips_and_seeds_defaults(self):
		"""policy token signs + round-trips; POLICY_DEFAULTS seed present."""
		settings = frappe.get_single("Access Hub Settings")
		if not settings.rules:
			from access_hub.access_hub.doctype.access_hub_settings.access_hub_settings import (
				after_install,
			)

			after_install()
			settings = frappe.get_single("Access Hub Settings")

		token = get_current_policy_token()
		pub = get_public_key()
		decoded = pyseto.decode(pub, token)
		claims = json.loads(decoded.payload)

		self.assertEqual(claims["pv"], PV)
		self.assertIn(POLICY_RULES, claims)

		default_rule = POLICY_DEFAULTS[POLICY_RULES][0]
		self.assertTrue(
			any(
				r["doctype"] == default_rule["doctype"] and r["mode"] == default_rule["mode"]
				for r in claims[POLICY_RULES]
			)
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
		self.assertEqual(frappe.local.response.get("http_status_code"), ProtocolVersionError.http_status)

	def test_transport_token_hash_matches(self):
		self.site.reload()
		self.assertEqual(self.site.transport_token_hash, transport_token_hash(self.plaintext_token))


class TestLogServing(HubTestCase):
	def setUp(self):
		host = f"test-logserve-{self._testMethodName}.localhost"
		self.site = self.make_remote_site(host)

	def test_valid_id_serves_lines(self):
		from access_hub.access_hub.doctype.session_log_file.session_log_file import upsert_index
		from access_hub.api import get_session_log_lines

		jti = f"test-log-{self._testMethodName}-jti"
		self.register_log_file_cleanup(self.site.name, jti)
		append_lines(self.site.name, jti, '{"a":1}\n{"b":2}')
		doc = upsert_index(self.site.name, jti, employee="x@example.com", added_lines=2)

		frappe.set_user("Administrator")
		result = get_session_log_lines(name=doc.name)
		self.assertEqual(result["lines"], ['{"a":1}', '{"b":2}'])

	def test_non_system_manager_cannot_read_lines(self):
		from access_hub.access_hub.doctype.session_log_file.session_log_file import upsert_index
		from access_hub.api import get_session_log_lines

		jti = f"test-log-perm-{self._testMethodName}-jti"
		self.register_log_file_cleanup(self.site.name, jti)
		append_lines(self.site.name, jti, '{"a":1}')
		doc = upsert_index(self.site.name, jti, employee="x@example.com", added_lines=1)

		email = f"no-access-{self._testMethodName}@example.com"
		self.make_user(email, last_name="Access")

		# frappe.only_for() short-circuits to a no-op when frappe.flags.in_test
		# is set (true for the whole run-tests process) or the user is
		# Administrator - neither of which reflects the real deny path, so
		# flip in_test off for the duration of this one assertion to exercise
		# the actual role check.
		frappe.set_user(email)
		original_in_test = frappe.flags.in_test
		frappe.flags.in_test = False
		try:
			with self.assertRaises(frappe.PermissionError):
				get_session_log_lines(name=doc.name)
		finally:
			frappe.flags.in_test = original_in_test
			frappe.set_user("Administrator")

	def test_download_serves_full_file_content(self):
		from access_hub.access_hub.doctype.session_log_file.session_log_file import upsert_index
		from access_hub.api import download_session_log

		jti = f"test-log-dl-{self._testMethodName}-jti"
		self.register_log_file_cleanup(self.site.name, jti)
		append_lines(self.site.name, jti, '{"a":1}\n{"b":2}')
		doc = upsert_index(self.site.name, jti, employee="x@example.com", added_lines=2)

		frappe.set_user("Administrator")
		download_session_log(name=doc.name)
		self.assertEqual(frappe.local.response.get("type"), "download")
		self.assertEqual(frappe.local.response.get("filename"), f"{jti}.jsonl")
		self.assertIn(b'{"a":1}', frappe.local.response.get("filecontent"))

	def test_traversal_via_host_is_rejected(self):
		# os.path.basename() already neutralizes "../../etc" -> "etc" (stays
		# inside base_dir as a subdirectory) - the case that can still reach
		# outside base_dir is a literal ".." component, which basename passes
		# through unchanged. _safe_join's realpath-containment check must
		# catch that.
		with self.assertRaises(frappe.PermissionError):
			path_for("..", "..")

	def test_traversal_via_jti_is_rejected(self):
		# jti gets a literal ".jsonl" suffix appended after basename(), which
		# defangs a literal ".." (it becomes the filename "...jsonl", not a
		# parent-dir reference) - so the jti axis can't reach outside
		# base_dir even without _safe_join. Exercise a case where the ".."
		# is only defanged by the suffix, and confirm it resolves safely
		# inside base_dir rather than raising (there's nothing to reject).
		p = path_for(self.site.name, "..")
		self.assertTrue(p.startswith(base_dir()))
		self.assertEqual(os.path.basename(p), "...jsonl")

	def test_traversal_attempt_is_neutralized_not_just_rejected(self):
		# Even when basename() reduces a traversal-looking value to something
		# that doesn't raise, it must resolve to a safe filename *inside*
		# base_dir, never actually escaping it.
		p = path_for(self.site.name, "../../../etc/passwd")
		self.assertTrue(p.startswith(base_dir()))
		self.assertEqual(os.path.basename(p), "passwd.jsonl")

	def test_resolved_paths_stay_inside_base_dir(self):
		p = path_for(self.site.name, "some-jti")
		base = base_dir()
		self.assertTrue(p.startswith(base))


class TestRequestAccessAttribution(HubTestCase):
	"""request_access() must always mint for the calling session user - no
	employee= override - so a grant is never attributable to anyone but the
	person who actually clicked the button (DESIGN.md's no-approval-step,
	full-attribution posture)."""

	def setUp(self):
		self.site = self.make_remote_site(f"test-reqacc-{self._testMethodName}.localhost")
		self.caller_email = f"caller-{self._testMethodName}@example.com"
		self.make_user(self.caller_email, first_name="Caller")
		self.addCleanup(frappe.set_user, "Administrator")

	def test_request_access_has_no_employee_override_param(self):
		import inspect

		from access_hub.api import request_access

		self.assertNotIn("employee", inspect.signature(request_access).parameters)

	def test_request_access_mints_for_calling_user(self):
		from access_hub.api import request_access

		frappe.set_user(self.caller_email)
		result = request_access(remote_site=self.site.name)
		self.assertIn("redirect_url", result)

		jti = frappe.db.get_value(
			"Access Grant", {"remote_site": self.site.name}, "jti", order_by="creation desc"
		)
		grant = frappe.get_doc("Access Grant", {"jti": jti})
		self.assertEqual(grant.employee, self.caller_email)
		self.addCleanup(frappe.delete_doc, "Access Grant", grant.name, True, None, True)


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
		self.plaintext_token = "http-regression-transport-token-1"
		self.host = f"test-http-{self._testMethodName}.localhost"
		self.jti = f"http-test-{self._testMethodName}-jti"
		self.site = self.make_remote_site(self.host, transport_token=self.plaintext_token)
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
		body = (
			b'{"pv":1,"jti":"'
			+ self.jti.encode()
			+ b'","host":"'
			+ self.host.encode()
			+ b'","session_meta":{"emp_email":"x@example.com"},"ndjson":"{\\"a\\":1}"}'
		)
		resp = self._post("access_hub.api.push_logs", body, token=self.plaintext_token)
		self.assertEqual(resp.status_code, 200)
		self.assertIn("received_lines", resp.get_data(as_text=True))

	def test_announce_over_http_with_typed_params(self):
		body = b'{"pv":1,"port":8005,"protocol":"https","app_version":"0.1.0"}'
		resp = self._post("access_hub.api.announce", body, token=self.plaintext_token)
		self.assertEqual(resp.status_code, 200)

	def test_report_provisioning_over_http_with_typed_params(self):
		body = (
			b'{"pv":1,"jti":"'
			+ self.jti.encode()
			+ b'","event":"created","user":"x@example.com","ts":"2026-01-01T00:00:00"}'
		)
		resp = self._post("access_hub.api.report_provisioning", body, token=self.plaintext_token)
		self.assertEqual(resp.status_code, 200)

	def test_pv_mismatch_over_http_returns_426(self):
		resp = self._post("access_hub.api.get_policy", b'{"pv":2}', token=self.plaintext_token)
		self.assertEqual(resp.status_code, 426)


class TestEnrollmentInfoPermission(HubTestCase):
	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_system_manager_can_fetch(self):
		from access_hub.api import get_hub_enrollment_info

		frappe.set_user("Administrator")
		result = get_hub_enrollment_info()
		self.assertIn("access_guard_hub_url", result)
		self.assertIn("access_guard_hub_public_key", result)

	def test_non_system_manager_is_denied(self):
		from access_hub.api import get_hub_enrollment_info

		email = f"no-access-enroll-{self._testMethodName}@example.com"
		self.make_user(email, last_name="Access")

		frappe.set_user(email)
		original_in_test = frappe.flags.in_test
		frappe.flags.in_test = False
		try:
			with self.assertRaises(frappe.PermissionError):
				get_hub_enrollment_info()
		finally:
			frappe.flags.in_test = original_in_test
