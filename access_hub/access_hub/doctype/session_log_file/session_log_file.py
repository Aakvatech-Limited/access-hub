# Copyright (c) 2026, Aakvatech Limited and contributors
# For license information, please see license.txt

import os

import frappe
from frappe.model.document import Document


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


def path_for(host: str, jti: str) -> str:
	"""Canonical on-disk path for a session's log file, base/<host>/<jti>.jsonl.
	`host` and `jti` are never taken as full paths — only ever joined as single
	path segments, so `../` in either cannot escape base_dir."""
	base = base_dir()
	host_component = os.path.basename(host)
	jti_component = os.path.basename(jti) + ".jsonl"
	return _safe_join(base, host_component, jti_component)


def append_lines(host: str, jti: str, ndjson: str) -> int:
	"""Append `ndjson` (one or more newline-separated JSON lines) to the file
	for (host, jti). Returns the number of lines appended."""
	path = path_for(host, jti)
	os.makedirs(os.path.dirname(path), exist_ok=True)

	lines = [line for line in ndjson.splitlines() if line.strip()]
	if not lines:
		return 0

	with open(path, "a", encoding="utf-8") as f:
		for line in lines:
			f.write(line)
			f.write("\n")

	return len(lines)


def upsert_index(remote_site: str, jti: str, employee: str | None = None, added_lines: int = 0):
	base = base_dir()
	path = path_for(remote_site, jti)
	rel_path = os.path.relpath(path, base)
	size_bytes = os.path.getsize(path) if os.path.exists(path) else 0

	if frappe.db.exists("Session Log File", {"jti": jti}):
		doc = frappe.get_doc("Session Log File", {"jti": jti})
		doc.size_bytes = size_bytes
		doc.line_count = (doc.line_count or 0) + added_lines
		if employee and not doc.employee:
			doc.employee = employee
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Session Log File",
				"remote_site": remote_site,
				"jti": jti,
				"employee": employee,
				"date": frappe.utils.today(),
				"path": rel_path,
				"size_bytes": size_bytes,
				"line_count": added_lines,
			}
		)
		doc.insert(ignore_permissions=True)
	return doc


def read_lines(name: str, start: int = 0, end: int | None = None) -> list[str]:
	"""Serve a log by its Session Log File doctype `name` (never a client-
	supplied path). Resolves the canonical path via path_for() (traversal
	guard baked in), then returns lines [start:end] (0-indexed, end exclusive).

	Callers must already be authorized (System Manager on the hub — this
	function performs no permission check of its own).
	"""
	doc = frappe.get_doc("Session Log File", name)
	path = path_for(doc.remote_site, doc.jti)

	if not os.path.exists(path):
		return []

	with open(path, encoding="utf-8") as f:
		lines = f.readlines()

	if end is None:
		end = len(lines)
	return [line.rstrip("\n") for line in lines[start:end]]
