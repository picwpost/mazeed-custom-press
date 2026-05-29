from __future__ import annotations

import frappe

from mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run import (
	ReleaseGroupScriptRun,
)


@frappe.whitelist()
def create_release_group_script_job(requested_benches=None, raw_script=None, timeout=None):
	benches = _normalize_requested_benches(requested_benches)

	if not benches:
		frappe.throw("requested_benches is required")

	if not raw_script:
		frappe.throw("raw_script is required")

	job = ReleaseGroupScriptRun.create(
		requested_benches=benches,
		raw_script=raw_script,
		timeout=timeout,
	)
	return {"job": job.name}


@frappe.whitelist()
def get_release_group_script_job_detail(job_id):
	return ReleaseGroupScriptRun.get_detail(job_id)


def _normalize_requested_benches(value) -> list[str]:
	if value is None:
		return []

	if isinstance(value, str):
		value = value.strip()
		if not value:
			return []
		try:
			value = frappe.parse_json(value)
		except Exception:
			return [part.strip() for part in value.split(",") if part.strip()]

	if not isinstance(value, list):
		frappe.throw("requested_benches must be a list")

	benches: list[str] = []
	for bench in value:
		if bench is None:
			continue
		bench_name = str(bench).strip()
		if bench_name and bench_name not in benches:
			benches.append(bench_name)
	return benches

