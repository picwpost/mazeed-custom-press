from __future__ import annotations

import frappe
from frappe.utils import cint
from press.api.site import protected

from mazeed_custom_press.release_rollout import create_release_rollout


@frappe.whitelist()
@protected("Release Group")
def update_all_sites(name):
	if rollout_queue_enabled():
		return create_release_rollout(name)
	return run_legacy_update_all_sites(name)


def rollout_queue_enabled() -> bool:
	# Code may be deployed before migrate on production. Missing schema must be safely off.
	if not frappe.get_meta("Press Settings").has_field("enable_release_rollout_queue"):
		return False
	return bool(frappe.utils.cint(frappe.db.get_single_value("Press Settings", "enable_release_rollout_queue")))


def run_legacy_update_all_sites(name):
	benches = frappe.get_all("Bench", {"group": name, "status": "Active"})
	for bench in benches:
		frappe.get_cached_doc("Bench", bench).update_all_sites()


def _check_rollout_access(rollout_name: str):
	from press.api.site import has_support_access
	from press.utils import get_current_team

	release_group = frappe.db.get_value("Release Rollout", rollout_name, "release_group")
	if not release_group:
		frappe.throw("Release Rollout not found", frappe.DoesNotExistError)
	user_type = frappe.session.data.user_type or frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type == "System User":
		return
	if frappe.db.get_value("Release Group", release_group, "team") == get_current_team():
		return
	if has_support_access("Release Group", release_group):
		return
	frappe.throw("Not Permitted", frappe.PermissionError)


@frappe.whitelist()
def cancel_rollout(name):
	from mazeed_custom_press.release_rollout import cancel_rollout as cancel

	_check_rollout_access(name)
	cancel(name)


@frappe.whitelist()
def pause_rollout(name):
	from mazeed_custom_press.release_rollout import pause_rollout as pause

	_check_rollout_access(name)
	pause(name)


@frappe.whitelist()
def resume_rollout(name):
	from mazeed_custom_press.release_rollout import resume_rollout as resume

	_check_rollout_access(name)
	resume(name)


@frappe.whitelist()
def get_rollout_summary(name):
	_check_rollout_access(name)
	doc = frappe.get_doc("Release Rollout", name)
	data = doc.as_dict(no_nulls=True)
	data.server_time = frappe.utils.now_datetime()
	data.completed_count = sum(cint(data.get(key)) for key in (
		"success_sites", "recovered_sites", "failed_sites", "skipped_sites", "cancelled_sites"
	))
	data.updated_sites = cint(data.get("success_sites")) + cint(data.get("recovered_sites"))
	# The displayed active count must never exceed the displayed concurrency limit (DASH-10).
	data.active_count = min(
		cint(data.get("starting_sites")) + cint(data.get("running_sites")),
		cint(data.get("max_concurrent_updates")),
	)
	data.progress_percent = (data.completed_count / cint(data.total_sites) * 100) if cint(data.total_sites) else 0
	return data


@frappe.whitelist()
def get_rollout_sites(name, status=None, stage=None, start=0, page_length=50):
	_check_rollout_access(name)
	filters = {"rollout": name}
	if status:
		filters["status"] = status
	if stage == "Canary":
		filters["is_canary"] = 1
	elif stage == "Main":
		filters["is_canary"] = 0
	page_length = min(max(cint(page_length), 1), 100)
	return frappe.get_all(
		"Release Rollout Site", filters=filters,
		fields=["name", "site", "source_bench", "status", "site_update", "is_canary", "last_error", "started_at", "finished_at"],
		order_by="FIELD(status, 'Running', 'Starting', 'Fatal', 'Skipped', 'Cancelled', 'Pending', 'Recovered', 'Success'), creation asc",
		start=cint(start), page_length=page_length,
	)
