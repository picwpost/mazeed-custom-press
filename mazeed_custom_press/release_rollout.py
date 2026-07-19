from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime


ELIGIBLE_SITE_STATUSES = ("Active", "Inactive", "Suspended")
TERMINAL_SITE_UPDATE_MAP = {
	"Success": "Success",
	"Recovered": "Recovered",
	"Fatal": "Fatal",
	"Cancelled": "Cancelled",
}
TERMINAL_ROLLOUT_SITE_STATUSES = tuple(TERMINAL_SITE_UPDATE_MAP.values()) + ("Skipped",)
SUCCESSFUL_STATUSES = ("Success", "Recovered")
FAILED_STATUSES = ("Fatal", "Skipped", "Cancelled")
STARTING_TIMEOUT_MINUTES = 10


def create_release_rollout(release_group: str, max_concurrent_updates=None, canary_size=None):
	# Serializes the active-rollout check without changing the Press DocType.
	if not frappe.db.get_value("Release Group", release_group, "name", for_update=True):
		frappe.throw(_("Release Group {0} does not exist").format(release_group))

	if frappe.db.exists(
		"Release Rollout", {"release_group": release_group, "status": ("in", ("Draft", "Running"))}
	):
		frappe.throw(_("An active rollout already exists for this Release Group"))

	benches = frappe.get_all(
		"Bench", filters={"group": release_group, "status": "Active"}, pluck="name", order_by="name"
	)
	sites = frappe.get_all(
		"Site",
		filters={"bench": ("in", benches), "status": ("in", ELIGIBLE_SITE_STATUSES)},
		fields=["name", "bench"],
		order_by="name",
	) if benches else []
	if not sites:
		frappe.throw(_("No eligible sites were found for this Release Group"))

	# Defaults come from Press Settings and are captured on the rollout at
	# creation; changing the settings later never affects a running rollout.
	# `or 2` would silently turn an invalid explicit 0 into the default and
	# bypass validation, so explicit arguments are validated as passed.
	if max_concurrent_updates is None:
		limit = cint(frappe.db.get_single_value("Press Settings", "rollout_max_concurrent_updates")) or 2
	else:
		limit = cint(max_concurrent_updates)
	if canary_size is None:
		# The implicit default clamps to the selection so a small release
		# group can still roll out; 0 in settings means "skip the gate".
		settings_canary = frappe.db.get_single_value("Press Settings", "rollout_canary_size")
		canaries = min(cint(settings_canary) if settings_canary is not None else 2, len(sites))
	else:
		canaries = cint(canary_size)
	if limit <= 0:
		frappe.throw(_("Max concurrent updates must be greater than zero"))
	if canaries < 0 or canaries > len(sites):
		frappe.throw(_("Canary size must be between 0 and {0}").format(len(sites)))

	now = now_datetime()
	rollout = frappe.get_doc({
		"doctype": "Release Rollout",
		"release_group": release_group,
		"status": "Running",
		"stage": "Canary" if canaries else "Main",
		"max_concurrent_updates": limit,
		"canary_size": canaries,
		"canary_status": "Pending" if canaries else "Passed",
		"canary_finished_at": None if canaries else now,
		"total_sites": len(sites),
		"pending_sites": len(sites),
		"started_at": now,
		"started_by": frappe.session.user,
	}).insert(ignore_permissions=True)

	for index, site in enumerate(sites):
		frappe.get_doc({
			"doctype": "Release Rollout Site",
			"rollout": rollout.name,
			"site": site.name,
			"source_bench": site.bench,
			"status": "Pending",
			"priority": 0,
			"is_canary": index < canaries,
		}).insert(ignore_permissions=True)

	frappe.enqueue(
		"mazeed_custom_press.release_rollout.start_next_sites",
		rollout_name=rollout.name,
		enqueue_after_commit=True,
	)
	return {"rollout": rollout.name, "selected_sites": len(sites)}


def start_next_sites(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status != "Running":
		return

	active = frappe.db.count(
		"Release Rollout Site", {"rollout": rollout.name, "status": ("in", ("Starting", "Running"))}
	)
	available = cint(rollout.max_concurrent_updates) - active
	if available <= 0:
		return

	filters = {"rollout": rollout.name, "status": "Pending", "is_canary": rollout.stage == "Canary"}
	rows = frappe.get_all(
		"Release Rollout Site", filters=filters, pluck="name", order_by="priority desc, creation asc", limit=available
	)
	for row_name in rows:
		frappe.db.sql(
			"UPDATE `tabRelease Rollout Site` SET status='Starting', modified=%s "
			"WHERE name=%s AND status='Pending'",
			(now_datetime(), row_name),
		)
		# MariaDB cursor rowcount is exposed through the transaction object only inconsistently;
		# the parent lock guarantees these selected rows remain ours in this transaction.
		frappe.enqueue(
			"mazeed_custom_press.release_rollout.start_rollout_site",
			rollout_site_name=row_name,
			enqueue_after_commit=True,
		)
	if rows and rollout.stage == "Canary" and rollout.canary_status == "Pending":
		frappe.db.set_value("Release Rollout", rollout.name, {
			"canary_status": "Running", "canary_started_at": rollout.canary_started_at or now_datetime(),
		})
	_recount(rollout.name)


def attach_rollout_site(doc, method=None):
	rollout_site = getattr(frappe.flags, "release_rollout_site", None)
	if rollout_site and not doc.release_rollout_site:
		doc.release_rollout_site = rollout_site


def start_rollout_site(rollout_site_name: str):
	row = frappe.get_doc("Release Rollout Site", rollout_site_name, for_update=True)
	if row.status != "Starting":
		return

	if frappe.db.get_value("Release Rollout", row.rollout, "status") != "Running":
		# Paused or cancelled after this row was claimed: release the claim so
		# the site is picked up again on resume instead of starting now.
		row.db_set("status", "Pending")
		return

	existing = frappe.db.get_value("Site Update", {"release_rollout_site": row.name}, "name")
	if existing:
		_mark_running(row, existing)
		return

	site = frappe.get_doc("Site", row.site)
	if site.status not in ELIGIBLE_SITE_STATUSES or site.bench != row.source_bench:
		_skip_row(row, "Site is no longer eligible or has moved to another bench")
		return

	try:
		frappe.flags.release_rollout_site = row.name
		site_update = site.schedule_update()
		_mark_running(row, site_update)
	except Exception as exc:
		# schedule_update may have inserted successfully before a later local write failed.
		# Never classify that case as skipped or create a second update on retry.
		existing = frappe.db.get_value("Site Update", {"release_rollout_site": row.name}, "name")
		if existing:
			_mark_running(row, existing)
			return
		frappe.log_error(
			title=f"Release rollout site failed: {row.name}",
			message=frappe.get_traceback(with_context=True),
		)
		_skip_row(row, str(exc))
	finally:
		frappe.flags.release_rollout_site = None


def _mark_running(row, site_update: str):
	row.db_set({"site_update": site_update, "status": "Running", "started_at": row.started_at or now_datetime()})
	_recount(row.rollout)


def _skip_row(row, message: str):
	row.db_set({"status": "Skipped", "last_error": (message or "Unknown error")[:1000], "finished_at": now_datetime()})
	_recount_and_advance(row.rollout)


def observe_agent_job(doc, method=None):
	if doc.job_type not in (
		"Update Site Migrate", "Update Site Pull", "Recover Failed Site Migrate",
		"Recover Failed Site Pull", "Recover Failed Site Update",
	):
		return
	updates = set(frappe.get_all("Site Update", {"update_job": doc.name}, pluck="name"))
	updates.update(frappe.get_all("Site Update", {"recover_job": doc.name}, pluck="name"))
	for update in updates:
		if frappe.db.exists("Release Rollout Site", {"site_update": update}):
			frappe.enqueue(
				"mazeed_custom_press.release_rollout.sync_site_update",
				site_update_name=update,
				enqueue_after_commit=True,
			)


def sync_site_update(site_update_name: str):
	status = frappe.db.get_value("Site Update", site_update_name, "status")
	rollout_status = TERMINAL_SITE_UPDATE_MAP.get(status)
	if not rollout_status:
		return
	row_name = frappe.db.get_value("Release Rollout Site", {"site_update": site_update_name}, "name")
	if not row_name:
		return
	row = frappe.get_doc("Release Rollout Site", row_name, for_update=True)
	if row.status in TERMINAL_ROLLOUT_SITE_STATUSES:
		return
	row.db_set({"status": rollout_status, "finished_at": now_datetime()})
	_recount_and_advance(row.rollout)


def _recount_and_advance(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	# Late completions on paused/cancelled rollouts must still update counters,
	# but only a Running rollout may promote, refill, or finish.
	_recount(rollout.name)
	if rollout.status != "Running":
		return
	if rollout.stage == "Canary":
		canary_statuses = frappe.get_all(
			"Release Rollout Site", {"rollout": rollout.name, "is_canary": 1}, pluck="status"
		)
		if any(status in FAILED_STATUSES for status in canary_statuses):
			now = now_datetime()
			frappe.db.set_value("Release Rollout", rollout.name, {
				"canary_status": "Failed", "canary_finished_at": now,
				"stage": "Finished", "status": "Completed With Failures", "finished_at": now,
			})
			frappe.db.sql(
				"UPDATE `tabRelease Rollout Site` SET status='Skipped', "
				"last_error=%s, finished_at=%s WHERE rollout=%s AND is_canary=0 AND status='Pending'",
				("Not started because the canary gate failed", now, rollout.name),
			)
			_recount(rollout.name)
			return
		if canary_statuses and all(status in SUCCESSFUL_STATUSES for status in canary_statuses):
			frappe.db.set_value("Release Rollout", rollout.name, {
				"canary_status": "Passed", "canary_finished_at": now_datetime(), "stage": "Main",
			})

	counts = _status_counts(rollout.name)
	if not counts.get("Pending") and not counts.get("Starting") and not counts.get("Running"):
		failed = any(counts.get(status) for status in FAILED_STATUSES)
		frappe.db.set_value("Release Rollout", rollout.name, {
			"status": "Completed With Failures" if failed else "Completed",
			"stage": "Finished", "finished_at": rollout.finished_at or now_datetime(),
		})
		return
	frappe.enqueue(
		"mazeed_custom_press.release_rollout.start_next_sites",
		rollout_name=rollout.name,
		enqueue_after_commit=True,
	)


def cancel_rollout(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status not in ("Running", "Paused"):
		frappe.throw(_("Only a running or paused rollout can be cancelled"))
	now = now_datetime()
	# Rows that never created a Site Update stop here. Rows already Running
	# drain naturally: an in-flight Agent job cannot be aborted safely, so
	# their results are still recorded, but nothing refills their slots.
	frappe.db.sql(
		"UPDATE `tabRelease Rollout Site` SET status='Cancelled', last_error=%s, finished_at=%s "
		"WHERE rollout=%s AND status IN ('Pending', 'Starting')",
		("Cancelled by operator before starting", now, rollout.name),
	)
	frappe.db.set_value("Release Rollout", rollout.name, {
		"status": "Cancelled", "stage": "Finished", "finished_at": rollout.finished_at or now,
	})
	_recount(rollout.name)


def pause_rollout(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status != "Running":
		frappe.throw(_("Only a running rollout can be paused"))
	frappe.db.set_value("Release Rollout", rollout.name, "status", "Paused")


def resume_rollout(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status != "Paused":
		frappe.throw(_("Only a paused rollout can be resumed"))
	frappe.db.set_value("Release Rollout", rollout.name, "status", "Running")
	# Reuses the normal advance path: recount, evaluate the canary gate,
	# finish if everything drained while paused, otherwise refill capacity.
	_recount_and_advance(rollout.name)


def reconcile_running_rollouts():
	for rollout_name in frappe.get_all("Release Rollout", {"status": "Running"}, pluck="name"):
		for update in frappe.get_all(
			"Release Rollout Site", {"rollout": rollout_name, "status": "Running", "site_update": ("is", "set")},
			pluck="site_update",
		):
			sync_site_update(update)

		cutoff = add_to_date(now_datetime(), minutes=-STARTING_TIMEOUT_MINUTES)
		for row_name in frappe.get_all(
			"Release Rollout Site", {"rollout": rollout_name, "status": "Starting", "modified": ("<", cutoff)},
			pluck="name",
		):
			update = frappe.db.get_value("Site Update", {"release_rollout_site": row_name}, "name")
			if update:
				frappe.db.set_value("Release Rollout Site", row_name, {"site_update": update, "status": "Running"})
				sync_site_update(update)
			else:
				frappe.db.set_value("Release Rollout Site", row_name, "status", "Pending")
		frappe.db.set_value("Release Rollout", rollout_name, "last_reconciled_at", now_datetime())
		_recount_and_advance(rollout_name)


def _lock_rollout(name: str):
	return frappe.get_doc("Release Rollout", name, for_update=True)


def _status_counts(rollout_name: str) -> dict[str, int]:
	rows = frappe.db.sql(
		"SELECT status, COUNT(*) AS count FROM `tabRelease Rollout Site` WHERE rollout=%s GROUP BY status",
		rollout_name,
		as_dict=True,
	)
	return {row.status: cint(row.count) for row in rows}


def _recount(rollout_name: str):
	counts = _status_counts(rollout_name)
	frappe.db.set_value("Release Rollout", rollout_name, {
		"pending_sites": counts.get("Pending", 0),
		"starting_sites": counts.get("Starting", 0),
		"running_sites": counts.get("Running", 0),
		"success_sites": counts.get("Success", 0),
		"recovered_sites": counts.get("Recovered", 0),
		"failed_sites": counts.get("Fatal", 0),
		"skipped_sites": counts.get("Skipped", 0),
		"cancelled_sites": counts.get("Cancelled", 0),
	}, update_modified=False)
