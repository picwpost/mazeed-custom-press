from __future__ import annotations

import frappe
from frappe.utils import cint
from press.api.site import protected

from mazeed_custom_press.release_rollout import create_release_rollout, logger


@frappe.whitelist()
@protected("Release Group")
def update_all_sites(name, source_bench=None, canary_sites=None):
	"""Start a rollout for a release group.

	`canary_sites` is optional and overrides canary selection for this rollout
	only. Omit it and selection falls to the sites flagged "Use as Rollout
	Canary", then to the first few by name. The dashboard button calls this
	without it, so its behaviour is unchanged.
	"""
	enabled = rollout_queue_enabled()
	logger.info(
		f"update_all_sites: release_group={name} source_bench={source_bench} "
		f"canary_sites={canary_sites} user={frappe.session.user} rollout_queue_enabled={enabled}"
	)
	if enabled:
		return create_release_rollout(name, source_bench=source_bench, canary_sites=canary_sites)
	if canary_sites:
		# The legacy path has no canary stage at all, so honouring the argument
		# is impossible. Say so rather than dropping it and updating everything
		# at once when the operator asked for a gated release.
		frappe.throw(
			"Canary sites require the release rollout queue. "
			"Enable it in Press Settings, or start the rollout without selecting canaries."
		)
	return run_legacy_update_all_sites(name, source_bench=source_bench)


@frappe.whitelist()
@protected("Release Group")
def get_canary_candidates(name, source_bench=None):
	"""Sites a rollout of this group could use as canaries, for a picker.

	Mirrors the selection create_release_rollout() will make, so what the
	operator sees is what the rollout would actually do.
	"""
	from mazeed_custom_press.release_rollout import CANARY_FLAG_FIELD, ELIGIBLE_SITE_STATUSES

	if source_bench:
		benches = [source_bench]
	else:
		benches = frappe.get_all(
			"Bench", filters={"group": name, "status": "Active"}, pluck="name", order_by="name"
		)
	if not benches:
		return []
	fields = ["name", "bench", "status"]
	if frappe.get_meta("Site").has_field(CANARY_FLAG_FIELD):
		fields.append(CANARY_FLAG_FIELD)
	sites = frappe.get_all(
		"Site",
		filters={"bench": ("in", benches), "status": ("in", ELIGIBLE_SITE_STATUSES)},
		fields=fields,
		order_by="name",
	)
	for site in sites:
		site["is_flagged_canary"] = bool(cint(site.pop(CANARY_FLAG_FIELD, 0)))
	return sites


def rollout_queue_enabled() -> bool:
	# Code may be deployed before migrate on production. Missing schema must be safely off.
	if not frappe.get_meta("Press Settings").has_field("enable_release_rollout_queue"):
		logger.info("rollout_queue_enabled: Press Settings has no 'enable_release_rollout_queue' field yet (migrate pending?) -- treating as off")
		return False
	value = frappe.db.get_single_value("Press Settings", "enable_release_rollout_queue")
	logger.info(f"rollout_queue_enabled: Press Settings.enable_release_rollout_queue={value!r}")
	return bool(frappe.utils.cint(value))


def run_legacy_update_all_sites(name, source_bench=None):
	from mazeed_custom_press.overrides.bench import call_original_update_all_sites

	if source_bench:
		# Scoped to the one bench clicked -- matches Press's original,
		# always-bench-scoped Bench.update_all_sites behavior.
		benches = [{"name": source_bench}]
	else:
		benches = frappe.get_all("Bench", {"group": name, "status": "Active"})
	logger.info(f"run_legacy_update_all_sites: release_group={name} benches={[b['name'] for b in benches]}")
	for bench in benches:
		call_original_update_all_sites(frappe.get_cached_doc("Bench", bench["name"]))


def _get_has_support_access():
	# Support-agent access is not present in every Press build this app runs
	# against (confirmed absent entirely -- no module, no function, under any
	# name -- in the fork currently deployed to production). Rather than hard
	# import a path that may not exist, degrade gracefully: without it, System
	# User and team-owner access (checked before this is ever called) still
	# work unaffected -- only the extra support-agent grant is unavailable.
	for module_path in ("press.access.support_access", "press.api.site"):
		try:
			module = __import__(module_path, fromlist=["has_support_access"])
			return module.has_support_access
		except (ModuleNotFoundError, ImportError, AttributeError):
			continue
	logger.info("_check_rollout_access: has_support_access not available in this Press build")
	return None


def _check_rollout_access(rollout_name: str):
	from press.utils import get_current_team

	release_group = frappe.db.get_value("Release Rollout", rollout_name, "release_group")
	if not release_group:
		frappe.throw("Release Rollout not found", frappe.DoesNotExistError)
	user_type = frappe.session.data.user_type or frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type == "System User":
		return
	if frappe.db.get_value("Release Group", release_group, "team") == get_current_team():
		return
	has_support_access = _get_has_support_access()
	if has_support_access and has_support_access("Release Group", release_group):
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
	data.update(get_queue_wait_stats(name))
	return data


def get_queue_wait_stats(rollout_name: str) -> dict:
	"""How long this rollout's sites actually spent queueing, not updating.

	The headline number for "is Pending -> Updating still slow": it is measured
	from the rollout's own sites rather than guessed, so a large median points
	at delivery or agent worker capacity, while a large max next to a small
	median points at one stuck server.
	"""
	waits = frappe.db.sql(
		"""SELECT TIMESTAMPDIFF(SECOND, su.update_start, aj.start) AS wait
		FROM `tabRelease Rollout Site` rrs
		JOIN `tabSite Update` su ON su.name = rrs.site_update
		JOIN `tabAgent Job` aj ON aj.name = su.update_job
		WHERE rrs.rollout = %s AND su.update_start IS NOT NULL AND aj.start IS NOT NULL""",
		rollout_name,
		pluck=True,
	)
	waits = sorted(cint(wait) for wait in waits if wait is not None and cint(wait) >= 0)
	if not waits:
		return {"queue_wait_median": None, "queue_wait_max": None, "queue_wait_samples": 0}
	middle = len(waits) // 2
	median = waits[middle] if len(waits) % 2 else (waits[middle - 1] + waits[middle]) // 2
	return {
		"queue_wait_median": median,
		"queue_wait_max": waits[-1],
		"queue_wait_samples": len(waits),
	}


# Wait between the Site Update being created and an agent worker actually
# picking the job up. This is the "Pending" the operator sees on the site, and
# it is queueing, not work -- so it is worth showing separately from the update
# itself rather than hiding both inside one duration.
QUEUE_WAIT_WARN_SECONDS = 60
QUEUE_WAIT_BAD_SECONDS = 300


@frappe.whitelist()
def get_rollout_sites(name, status=None, stage=None, start=0, page_length=50):
	_check_rollout_access(name)
	conditions = ["rrs.rollout = %(rollout)s"]
	values = {"rollout": name}
	if status:
		conditions.append("rrs.status = %(status)s")
		values["status"] = status
	if stage == "Canary":
		conditions.append("rrs.is_canary = 1")
	elif stage == "Main":
		conditions.append("rrs.is_canary = 0")

	values["page_length"] = min(max(cint(page_length), 1), 100)
	values["start"] = cint(start)

	# Joined rather than stored on the row: the numbers stay correct for a
	# still-running update, and there is no second copy of the truth to drift.
	# Bounded by page_length, so at most 100 joined rows per call.
	rows = frappe.db.sql(
		f"""SELECT
			rrs.name, rrs.site, rrs.source_bench, rrs.status, rrs.site_update,
			rrs.is_canary, rrs.last_error, rrs.started_at, rrs.finished_at,
			s.status AS site_status,
			su.update_start, su.update_duration, su.deploy_type, su.skipped_backups,
			su.status AS site_update_status,
			aj.start AS job_start, aj.end AS job_end, aj.retry_count
		FROM `tabRelease Rollout Site` rrs
		LEFT JOIN `tabSite` s ON s.name = rrs.site
		LEFT JOIN `tabSite Update` su ON su.name = rrs.site_update
		LEFT JOIN `tabAgent Job` aj ON aj.name = su.update_job
		WHERE {" AND ".join(conditions)}
		ORDER BY FIELD(rrs.status, 'Running', 'Starting', 'Fatal', 'Skipped',
			'Cancelled', 'Pending', 'Recovered', 'Success'), rrs.creation ASC
		LIMIT %(start)s, %(page_length)s""",
		values,
		as_dict=True,
	)
	now = frappe.utils.now_datetime()
	for row in rows:
		_annotate_timings(row, now)
	return rows


def _annotate_timings(row, now):
	"""Split the elapsed time into queue wait and actual update work.

	Agent Job.start is the agent's own timestamp for when a worker began, so
	the gap before it is genuine queueing -- press-side delivery plus the wait
	for a free agent worker -- and not something the update could have avoided.
	"""
	row["queue_seconds"] = _seconds_between(row.get("update_start"), row.get("job_start"))
	if row.get("update_duration"):
		row["update_seconds"] = cint(row["update_duration"])
	else:
		# Still in flight: measure against now so the figure ticks up live.
		row["update_seconds"] = _seconds_between(row.get("job_start"), row.get("job_end") or now)

	# Nothing has been delivered yet, so the whole elapsed time is queueing.
	if row["queue_seconds"] is None and row.get("update_start") and not row.get("job_start"):
		row["queue_seconds"] = _seconds_between(row["update_start"], now)

	queue_seconds = row["queue_seconds"]
	if queue_seconds is None:
		row["queue_severity"] = None
	elif queue_seconds >= QUEUE_WAIT_BAD_SECONDS:
		row["queue_severity"] = "bad"
	elif queue_seconds >= QUEUE_WAIT_WARN_SECONDS:
		row["queue_severity"] = "warn"
	else:
		row["queue_severity"] = "ok"


def _seconds_between(start, end):
	if not start or not end:
		return None
	seconds = frappe.utils.time_diff_in_seconds(end, start)
	return cint(seconds) if seconds and seconds >= 0 else 0
