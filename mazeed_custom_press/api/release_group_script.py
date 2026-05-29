from __future__ import annotations

import json

import frappe

from mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run import (
	ReleaseGroupScriptRun,
)


def create_release_group_script_job():
	payload = _get_request_payload()
	requested_benches = _normalize_requested_benches(
		payload.get("requested_benches") or payload.get("benches")
	)
	raw_script = payload.get("raw_script") or payload.get("script")
	timeout = payload.get("timeout")

	if not requested_benches:
		frappe.throw("requested_benches is required")

	if not raw_script:
		frappe.throw("raw_script is required")

	job = ReleaseGroupScriptRun.create(
		requested_benches=requested_benches,
		raw_script=raw_script,
		timeout=timeout,
	)
	return {"job": job.name}


def get_release_group_script_job_detail(job_id: str | int):
	return ReleaseGroupScriptRun.get_detail(job_id)


def _get_request_payload() -> dict:
	payload: dict = {}

	if hasattr(frappe, "form_dict") and frappe.form_dict:
		payload.update(dict(frappe.form_dict))

	data = getattr(frappe.local.request, "data", b"") if hasattr(frappe.local, "request") else b""
	if data:
		if isinstance(data, bytes):
			data = data.decode("utf-8")
		data = data.strip()
		if data:
			try:
				payload.update(frappe.parse_json(data))
			except Exception:
				frappe.throw("Invalid JSON payload")

	return payload


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

