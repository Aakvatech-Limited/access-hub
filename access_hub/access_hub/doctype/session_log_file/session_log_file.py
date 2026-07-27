# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import os

import frappe
from frappe.model.document import Document

SUFFIX_BY_KIND = {"request": ".log", "doc": ".doc.log"}


class SessionLogFile(Document):
	pass


def base_dir() -> str:
	"""Root directory for pushed session log files: <site>/private/files/access_hub_logs/."""
	path = frappe.get_site_path("private", "files", "access_hub_logs")
	os.makedirs(path, exist_ok=True)
	return os.path.realpath(path)


def _safe_join(base: str, *parts: str) -> str:
	"""Join `parts` onto `base` and assert the resolved path is still inside
	`base` — the traversal guard. Raises on any escape attempt."""
	candidate = os.path.realpath(os.path.join(base, *parts))
	base_real = os.path.realpath(base)
	if not (candidate == base_real or candidate.startswith(base_real + os.sep)):
		frappe.throw("Invalid path.", frappe.PermissionError)
	return candidate


def path_for(remote_site: str, jti: str, log_kind: str = "request") -> str:
	"""Canonical on-disk path for a session's log file of the given kind,
	base/<remote_site>/<jti><suffix> — request: .log, doc: .doc.log.
	`remote_site` and `jti` are never taken as full paths — only ever joined
	as single path segments, so `../` in either cannot escape base_dir."""
	base = base_dir()
	site_component = os.path.basename(remote_site)
	jti_component = os.path.basename(jti) + SUFFIX_BY_KIND[log_kind]
	return _safe_join(base, site_component, jti_component)


def append_lines(remote_site: str, jti: str, log_kind: str, lines_text: str) -> int:
	"""Append `lines_text` (one or more newline-separated log lines) to the
	file for (remote_site, jti, log_kind). Returns the number of lines appended."""
	path = path_for(remote_site, jti, log_kind)
	os.makedirs(os.path.dirname(path), exist_ok=True)

	lines = [line for line in lines_text.splitlines() if line.strip()]
	if not lines:
		return 0

	with open(path, "a", encoding="utf-8") as f:
		for line in lines:
			f.write(line)
			f.write("\n")

	return len(lines)


def upsert_index(remote_site: str, jti: str, log_kind: str = "request", added_lines: int = 0):
	"""One row per (jti, log_kind) — a session's request log and doc-change
	log are tracked (and downloadable) independently. user_email is read from
	the matching Access Grant (the hub's own record), not sent by the guard -
	jti is already the key needed to look it up."""
	base = base_dir()
	path = path_for(remote_site, jti, log_kind)
	rel_path = os.path.relpath(path, base)
	size_bytes = os.path.getsize(path) if os.path.exists(path) else 0

	filters = {"jti": jti, "log_kind": log_kind}
	if frappe.db.exists("Session Log File", filters):
		doc = frappe.get_doc("Session Log File", filters)
		doc.size_bytes = size_bytes
		doc.line_count = (doc.line_count or 0) + added_lines
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Session Log File",
				"remote_site": remote_site,
				"jti": jti,
				"log_kind": log_kind,
				"date": frappe.utils.today(),
				"path": rel_path,
				"size_bytes": size_bytes,
				"line_count": added_lines,
			}
		)

	if not doc.user_email:
		doc.user_email = frappe.db.get_value("Access Grant", jti, "user_email")
	doc.save(ignore_permissions=True)
	return doc
