from __future__ import annotations

import logging

import frappe
from frappe import _
from frappe.model.document import bulk_insert
from frappe.utils import add_to_date, cint, now_datetime

logger = frappe.logger("mazeed_rollout", allow_site=True, file_count=20)
# frappe.logger() defaults to WARNING (dev) / ERROR (production), which would
# silently drop every .info() call below. This is a dedicated troubleshooting
# logger, so raise its own level explicitly rather than the bench-wide default.
logger.setLevel(logging.INFO)

ELIGIBLE_SITE_STATUSES = ("Active", "Inactive", "Suspended")
# Custom field on Site, added by install.py. A standing preference that survives
# across rollouts, as opposed to the per-rollout `canary_sites` argument.
CANARY_FLAG_FIELD = "use_as_rollout_canary"
# Mirrors press.press.doctype.site_update.site_update.SiteUpdate.has_pending_updates --
# any Site Update in one of these statuses blocks Press from creating another one for
# the same site, so this is also exactly the set worth adopting rather than racing.
ADOPTABLE_SITE_UPDATE_STATUSES = ("Pending", "Running", "Scheduled", "Recovering", "Failure")
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


def create_release_rollout(
	release_group: str,
	source_bench: str | None = None,
	max_concurrent_updates=None,
	canary_size=None,
	skip_backups_for_main_stage=None,
	max_concurrent_updates_per_server=None,
	canary_sites=None,
):
	logger.info(f"create_release_rollout: start release_group={release_group} source_bench={source_bench}")

	# Serializes the active-rollout check without changing the Press DocType.
	if not frappe.db.get_value("Release Group", release_group, "name", for_update=True):
		logger.info(f"create_release_rollout: abort release_group={release_group} reason=does_not_exist")
		frappe.throw(_("Release Group {0} does not exist").format(release_group))

	existing = frappe.db.get_value(
		"Release Rollout", {"release_group": release_group, "status": ("in", ("Draft", "Running"))}, "name"
	)
	if existing:
		logger.info(
			f"create_release_rollout: abort release_group={release_group} "
			f"reason=active_rollout_exists existing_rollout={existing}"
		)
		frappe.throw(_("An active rollout already exists for this Release Group"))

	if source_bench:
		# Scoped to the one bench the operator clicked -- this is what "Update
		# All Sites" has always meant (moving THIS bench's sites onward), not
		# every active bench in the release group. Pulling in unrelated
		# benches silently touched sites nobody asked to move (DASH-14).
		if frappe.db.get_value("Bench", source_bench, "group") != release_group:
			frappe.throw(_("Bench {0} does not belong to Release Group {1}").format(source_bench, release_group))
		benches = [source_bench]
	else:
		# No specific bench given: only reachable via the release-group-level
		# whitelisted endpoint (press.api.bench.update_all_sites called
		# directly by name), which has always covered every active bench.
		benches = frappe.get_all(
			"Bench", filters={"group": release_group, "status": "Active"}, pluck="name", order_by="name"
		)
	logger.info(f"create_release_rollout: release_group={release_group} benches={benches}")
	sites = frappe.get_all(
		"Site",
		filters={"bench": ("in", benches), "status": ("in", ELIGIBLE_SITE_STATUSES)},
		fields=["name", "bench"],
		order_by="name",
	) if benches else []
	if not sites:
		logger.info(f"create_release_rollout: abort release_group={release_group} reason=no_eligible_sites")
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
	if max_concurrent_updates_per_server is None:
		per_server_limit = cint(
			frappe.db.get_single_value("Press Settings", "rollout_max_concurrent_updates_per_server")
		)
	else:
		per_server_limit = cint(max_concurrent_updates_per_server)
	if limit <= 0:
		frappe.throw(_("Max concurrent updates must be greater than zero"))
	if per_server_limit < 0:
		frappe.throw(_("Max concurrent updates per server cannot be negative"))
	# 0 means "no separate per-server cap": use the rollout total, which is
	# exactly how scheduling behaved before this field existed. Anything else
	# would silently throttle rollouts that had already raised the total, and
	# since most release groups sit on a single server that would cut
	# throughput instead of raising it.
	per_server_limit = per_server_limit or limit
	if canaries < 0 or canaries > len(sites):
		frappe.throw(_("Canary size must be between 0 and {0}").format(len(sites)))
	canary_names = _resolve_canary_sites(
		sites=sites,
		benches=benches,
		canary_sites=canary_sites,
		canary_size_was_explicit=canary_size is not None,
		fallback_count=canaries,
	)
	canaries = len(canary_names)
	if skip_backups_for_main_stage is None:
		skip_backups = bool(cint(frappe.db.get_single_value("Press Settings", "rollout_skip_backups_for_main_stage")))
	else:
		skip_backups = bool(skip_backups_for_main_stage)

	now = now_datetime()
	rollout = frappe.get_doc({
		"doctype": "Release Rollout",
		"release_group": release_group,
		"status": "Running",
		"stage": "Canary" if canaries else "Main",
		"max_concurrent_updates": limit,
		"max_concurrent_updates_per_server": per_server_limit,
		"canary_size": canaries,
		"skip_backups_for_main_stage": skip_backups,
		"canary_status": "Pending" if canaries else "Passed",
		"canary_finished_at": None if canaries else now,
		"total_sites": len(sites),
		"pending_sites": len(sites),
		"started_at": now,
		"started_by": frappe.session.user,
	}).insert(ignore_permissions=True)

	# One insert() per site holds the Release Group's row lock for the whole
	# loop -- at 30k sites that's 30k round trips before the lock is ever
	# released. bulk_insert() builds the same rows as a handful of multi-row
	# INSERT statements instead; the doctype has no custom validate/hooks
	# (plain Document subclass), so skipping the per-row Document lifecycle
	# changes nothing here.
	rollout_site_docs = []
	for site in sites:
		doc = frappe.get_doc({
			"doctype": "Release Rollout Site",
			"rollout": rollout.name,
			"site": site.name,
			"source_bench": site.bench,
			"status": "Pending",
			"priority": 0,
			"is_canary": site.name in canary_names,
		})
		doc.name = frappe.generate_hash(length=10)
		rollout_site_docs.append(doc)
	bulk_insert("Release Rollout Site", rollout_site_docs)

	logger.info(
		f"create_release_rollout: created rollout={rollout.name} release_group={release_group} "
		f"sites={len(sites)} canaries={canaries} max_concurrent_updates={limit} "
		f"max_concurrent_updates_per_server={per_server_limit} skip_backups_for_main_stage={skip_backups} "
		f"canary_sites={sorted(canary_names)}"
	)
	_scale_burst_workers(rollout.name, start=True)
	frappe.enqueue(
		"mazeed_custom_press.release_rollout.start_next_sites",
		rollout_name=rollout.name,
		queue="short",
		enqueue_after_commit=True,
	)
	return {"rollout": rollout.name, "selected_sites": len(sites)}


def _resolve_canary_sites(
	sites: list,
	benches: list[str],
	canary_sites,
	canary_size_was_explicit: bool,
	fallback_count: int,
) -> set[str]:
	"""Decide which sites gate this rollout.

	Three sources, in descending precedence:

	1. `canary_sites` -- named for this one rollout. Validated strictly: a site
	   the operator asked for by name and did not get would gate the release on
	   sites they never chose, which defeats the point of choosing.
	2. Sites standing-flagged with CANARY_FLAG_FIELD, intersected with this
	   rollout's selection.
	3. The first `fallback_count` sites by name -- the original behaviour, kept
	   so a group with neither an argument nor a flag rolls out as it always has.
	"""
	eligible = {site.name for site in sites}

	if canary_sites is not None:
		requested = _parse_site_names(canary_sites)
		if not requested:
			frappe.throw(_("No canary sites were provided"))
		if canary_size_was_explicit:
			frappe.throw(
				_("Pass either canary_sites or canary_size, not both -- the size of an explicit list is the list.")
			)
		_reject_ineligible_canaries(requested, eligible, benches)
		logger.info(f"_resolve_canary_sites: source=explicit sites={sorted(requested)}")
		return requested

	flagged = _flagged_canary_sites(eligible)
	if flagged:
		logger.info(f"_resolve_canary_sites: source=site_flag sites={sorted(flagged)}")
		return flagged

	positional = {site.name for site in sites[:fallback_count]}
	logger.info(
		f"_resolve_canary_sites: source=first_{fallback_count}_by_name sites={sorted(positional)}"
	)
	return positional


def _parse_site_names(canary_sites) -> set[str]:
	"""Accept a list, or the JSON string the HTTP layer delivers one as."""
	if isinstance(canary_sites, str):
		canary_sites = frappe.parse_json(canary_sites)
	if isinstance(canary_sites, str):
		canary_sites = [canary_sites]
	if not isinstance(canary_sites, (list, tuple, set)):
		frappe.throw(_("Canary sites must be a list of site names"))
	return {str(name).strip() for name in canary_sites if str(name).strip()}


def _reject_ineligible_canaries(requested: set[str], eligible: set[str], benches: list[str]):
	"""Refuse the whole rollout if any named canary is not in the selection.

	Reports every bad site with its reason in one throw rather than failing on
	the first: an operator fixing a list of five wants to see all five problems,
	not to rerun four times.
	"""
	missing = sorted(requested - eligible)
	if not missing:
		return
	reasons = [f"{name} -- {_describe_ineligibility(name, benches)}" for name in missing]
	frappe.throw(
		_("These sites cannot be used as canaries:\n\n{0}").format("\n".join(reasons)),
		title=_("Canary sites are not eligible"),
	)


def _describe_ineligibility(site: str, benches: list[str]) -> str:
	if not frappe.db.exists("Site", site):
		return _("no such site")
	status, bench = frappe.db.get_value("Site", site, ["status", "bench"])
	if status not in ELIGIBLE_SITE_STATUSES:
		return _("status is {0}, must be one of {1}").format(status, ", ".join(ELIGIBLE_SITE_STATUSES))
	if bench not in benches:
		return _("on bench {0}, which this rollout does not cover").format(bench)
	return _("not part of this rollout")


def _flagged_canary_sites(eligible: set[str]) -> set[str]:
	"""Standing-flagged canary sites that are actually in this rollout.

	The flag lives on Site and is global, while a rollout covers one release
	group, so most flagged sites will not appear here -- that is an
	intersection, not an error. It is also why a stale flag can never block a
	deploy: an archived or moved-out site quietly stops being picked.
	"""
	if not frappe.get_meta("Site").has_field(CANARY_FLAG_FIELD):
		# Code can reach production before its migrate; no field means no flags.
		logger.info(f"_flagged_canary_sites: Site has no {CANARY_FLAG_FIELD} field yet (migrate pending?)")
		return set()
	flagged = set(frappe.get_all("Site", filters={CANARY_FLAG_FIELD: 1}, pluck="name"))
	dropped = flagged - eligible
	if dropped:
		logger.info(f"_flagged_canary_sites: ignoring flagged sites outside this rollout: {sorted(dropped)}")
	return flagged & eligible


def start_next_sites(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status != "Running":
		logger.info(f"start_next_sites: rollout={rollout_name} skipped status={rollout.status}")
		return

	is_canary = rollout.stage == "Canary"
	active_per_server = _active_counts_by_server(rollout.name)
	active = sum(active_per_server.values())
	available = cint(rollout.max_concurrent_updates) - active
	# Falls back to the total when unset, which is how rollouts created before
	# this field existed keep behaving exactly as they did. .get() rather than
	# attribute access so a deploy that lands this code before its migrate runs
	# degrades to the old single-cap behaviour instead of raising on every tick.
	per_server_limit = cint(rollout.get("max_concurrent_updates_per_server")) or cint(
		rollout.max_concurrent_updates
	)
	logger.info(
		f"start_next_sites: rollout={rollout_name} stage={rollout.stage} "
		f"active={active} max_concurrent_updates={rollout.max_concurrent_updates} "
		f"per_server_limit={per_server_limit} active_per_server={active_per_server} "
		f"available_slots={available}"
	)
	if available <= 0:
		return

	rows = _pick_batch(rollout.name, is_canary, available, per_server_limit, active_per_server)
	logger.info(f"start_next_sites: rollout={rollout_name} batch_picked={rows}")
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
			queue="short",
			enqueue_after_commit=True,
		)
	if rows and rollout.stage == "Canary" and rollout.canary_status == "Pending":
		frappe.db.set_value("Release Rollout", rollout.name, {
			"canary_status": "Running", "canary_started_at": rollout.canary_started_at or now_datetime(),
		})
	_recount(rollout.name)


# Server comes from a join on Bench rather than a column on the row, so
# rollouts already in flight when this shipped keep working with no backfill.
# LEFT JOIN with COALESCE, never an inner join on b.server: a bench whose
# server is unset -- or a source_bench row that has since been deleted -- would
# otherwise match no bucket at all and make those sites permanently
# unschedulable, silently stalling the rollout. They collapse into one unnamed
# bucket instead, which is scheduled like any other server.
_SERVER_OF_ROW = "COALESCE(b.server, '')"
_ROWS_WITH_SERVER = (
	"`tabRelease Rollout Site` rrs LEFT JOIN `tabBench` b ON b.name = rrs.source_bench"
)


def _active_counts_by_server(rollout_name: str) -> dict[str, int]:
	"""Slots this rollout is already using, per server."""
	rows = frappe.db.sql(
		f"""SELECT {_SERVER_OF_ROW} AS server, COUNT(*) AS count
		FROM {_ROWS_WITH_SERVER}
		WHERE rrs.rollout = %s AND rrs.status IN ('Starting', 'Running')
		GROUP BY {_SERVER_OF_ROW}""",
		rollout_name,
		as_dict=True,
	)
	return {row.server: cint(row.count) for row in rows}


def _servers_with_pending_sites(rollout_name: str, is_canary: bool) -> list[str]:
	return frappe.db.sql_list(
		f"""SELECT DISTINCT {_SERVER_OF_ROW}
		FROM {_ROWS_WITH_SERVER}
		WHERE rrs.rollout = %s AND rrs.status = 'Pending' AND rrs.is_canary = %s""",
		(rollout_name, cint(is_canary)),
	)


def _pending_rows_on_server(rollout_name: str, is_canary: bool, server: str, limit: int) -> list[str]:
	return frappe.db.sql_list(
		f"""SELECT rrs.name
		FROM {_ROWS_WITH_SERVER}
		WHERE rrs.rollout = %s AND rrs.status = 'Pending' AND rrs.is_canary = %s
			AND {_SERVER_OF_ROW} = %s
		ORDER BY rrs.priority DESC, rrs.creation ASC
		LIMIT %s""",
		(rollout_name, cint(is_canary), server, cint(limit)),
	)


def _pick_batch(
	rollout_name: str,
	is_canary: bool,
	available: int,
	per_server_limit: int,
	active_per_server: dict[str, int],
) -> list[str]:
	"""Fill the free slots without letting one server hog them.

	Candidates are selected per server rather than in one ordered query: a
	single query limited to the free slots could return nothing but rows for a
	server that is already at its cap, and the rollout would stall with other
	servers sitting idle. Least-busy server first, so capacity spreads instead
	of piling onto whichever server happens to sort first.
	"""
	servers = sorted(
		_servers_with_pending_sites(rollout_name, is_canary),
		key=lambda server: (active_per_server.get(server, 0), server),
	)
	rows = []
	for server in servers:
		if available <= 0:
			break
		server_available = min(per_server_limit - active_per_server.get(server, 0), available)
		if server_available <= 0:
			continue
		picked = _pending_rows_on_server(rollout_name, is_canary, server, server_available)
		rows.extend(picked)
		available -= len(picked)
	return rows


def attach_rollout_site(doc, method=None):
	rollout_site = getattr(frappe.flags, "release_rollout_site", None)
	if rollout_site and not doc.release_rollout_site:
		doc.release_rollout_site = rollout_site


def start_rollout_site(rollout_site_name: str):
	row = frappe.get_doc("Release Rollout Site", rollout_site_name, for_update=True)
	if row.status != "Starting":
		logger.info(f"start_rollout_site: {rollout_site_name} skipped status={row.status}")
		return

	if frappe.db.get_value("Release Rollout", row.rollout, "status") != "Running":
		# Paused or cancelled after this row was claimed: release the claim so
		# the site is picked up again on resume instead of starting now.
		logger.info(f"start_rollout_site: {rollout_site_name} rollout={row.rollout} not running, releasing claim")
		row.db_set("status", "Pending")
		return

	existing = frappe.db.get_value("Site Update", {"release_rollout_site": row.name}, "name")
	if existing:
		logger.info(f"start_rollout_site: {rollout_site_name} site_update={existing} already exists, reusing")
		_mark_running(row, existing)
		return

	site = frappe.get_doc("Site", row.site)
	if site.status not in ELIGIBLE_SITE_STATUSES or site.bench != row.source_bench:
		logger.info(
			f"start_rollout_site: {rollout_site_name} site={row.site} skipped "
			f"site_status={site.status} site_bench={site.bench} expected_bench={row.source_bench}"
		)
		_skip_row(row, "Site is no longer eligible or has moved to another bench")
		return

	# Press's own deploy flow can schedule a Site Update for this site on its own:
	# once a new Bench finishes building and goes Active, Bench.process_new_bench_job_update
	# calls Bench Update.update_sites_on_server for whichever sites the operator selected
	# in the deploy dialog -- independently of this rollout, and often minutes after the
	# deploy click. If that already claimed this site, adopt its Site Update instead of
	# racing schedule_update() against it (which would just lose to Press's own
	# has_pending_updates() guard and land here as a skip for no real reason).
	unclaimed = _find_unclaimed_pending_update(row.site)
	if unclaimed:
		logger.info(
			f"start_rollout_site: {rollout_site_name} site={row.site} adopting "
			f"pre-existing site_update={unclaimed} scheduled outside this rollout"
		)
		_mark_running(row, unclaimed)
		return

	# Canary sites always keep their backup, regardless of the setting -- they
	# are the ones proving the update is safe in the first place. Skipping
	# only applies once a site is past that gate. This is the one-off backup
	# taken immediately before this specific update; the regular scheduled
	# site backup system is entirely separate and is never affected.
	skip_backups = (
		not row.is_canary
		and bool(frappe.db.get_value("Release Rollout", row.rollout, "skip_backups_for_main_stage"))
	)
	try:
		frappe.flags.release_rollout_site = row.name
		site_update = site.schedule_update(skip_backups=skip_backups)
		logger.info(
			f"start_rollout_site: {rollout_site_name} site={row.site} scheduled "
			f"site_update={site_update} skip_backups={skip_backups}"
		)
		_mark_running(row, site_update)
	except Exception as exc:
		# schedule_update may have inserted successfully before a later local write failed.
		# Never classify that case as skipped or create a second update on retry.
		existing = frappe.db.get_value("Site Update", {"release_rollout_site": row.name}, "name")
		if existing:
			logger.info(
				f"start_rollout_site: {rollout_site_name} site_update={existing} "
				f"created despite error, reusing: {exc}"
			)
			_mark_running(row, existing)
			return
		# Narrow TOCTOU window: Press's deploy-triggered auto-update could have
		# claimed this site in the moment between the upfront check above and this
		# schedule_update() call. Same recovery, not a real failure.
		unclaimed = _find_unclaimed_pending_update(row.site)
		if unclaimed:
			logger.info(
				f"start_rollout_site: {rollout_site_name} site={row.site} adopting "
				f"pre-existing site_update={unclaimed} after a scheduling race: {exc}"
			)
			_mark_running(row, unclaimed)
			return
		logger.info(f"start_rollout_site: {rollout_site_name} site={row.site} failed: {exc}")
		frappe.log_error(
			title=f"Release rollout site failed: {row.name}",
			message=frappe.get_traceback(with_context=True),
		)
		_skip_row(row, str(exc))
	finally:
		frappe.flags.release_rollout_site = None


def _find_unclaimed_pending_update(site: str) -> str | None:
	"""A Site Update for this site that already exists outside this rollout --
	created by Press's own deploy-triggered auto-update -- and isn't tracked by
	any Release Rollout Site row yet. Most recent first, since a site can only
	have one truly active update at a time; older ones would be stale rows
	Press itself would already refuse to touch."""
	candidates = frappe.get_all(
		"Site Update",
		filters={"site": site, "status": ("in", ADOPTABLE_SITE_UPDATE_STATUSES)},
		pluck="name",
		order_by="creation desc",
	)
	for candidate in candidates:
		if not frappe.db.exists("Release Rollout Site", {"site_update": candidate}):
			return candidate
	return None


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
				queue="short",
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
	logger.info(f"sync_site_update: site_update={site_update_name} row={row_name} status={status} -> {rollout_status}")
	row.db_set({"status": rollout_status, "finished_at": now_datetime()})
	_recount_and_advance(row.rollout)


def _recount_and_advance(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	# Late completions on paused/cancelled rollouts must still update counters,
	# but only a Running rollout may promote, refill, or finish.
	counts = _recount(rollout.name)
	if rollout.status != "Running":
		logger.info(f"_recount_and_advance: rollout={rollout_name} not running (status={rollout.status}), stop")
		return
	if rollout.stage == "Canary":
		canary_statuses = frappe.get_all(
			"Release Rollout Site", {"rollout": rollout.name, "is_canary": 1}, pluck="status"
		)
		if any(status in FAILED_STATUSES for status in canary_statuses):
			logger.info(f"_recount_and_advance: rollout={rollout_name} canary FAILED statuses={canary_statuses}")
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
			# The bulk UPDATE above changed site rows out from under the counts
			# computed above, so this specific recount cannot reuse them.
			_recount(rollout.name)
			_scale_burst_workers(rollout.name, start=False)
			return
		if canary_statuses and all(status in SUCCESSFUL_STATUSES for status in canary_statuses):
			logger.info(f"_recount_and_advance: rollout={rollout_name} canary PASSED, advancing stage to Main")
			frappe.db.set_value("Release Rollout", rollout.name, {
				"canary_status": "Passed", "canary_finished_at": now_datetime(), "stage": "Main",
			})
			# Only the rollout's own canary_status/stage changed above -- no
			# site row did, so `counts` from the top of this call is still
			# accurate and does not need to be re-queried.

	if not counts.get("Pending") and not counts.get("Starting") and not counts.get("Running"):
		failed = any(counts.get(status) for status in FAILED_STATUSES)
		logger.info(f"_recount_and_advance: rollout={rollout_name} FINISHED counts={counts} failed={bool(failed)}")
		frappe.db.set_value("Release Rollout", rollout.name, {
			"status": "Completed With Failures" if failed else "Completed",
			"stage": "Finished", "finished_at": rollout.finished_at or now_datetime(),
		})
		_scale_burst_workers(rollout.name, start=False)
		return
	logger.info(f"_recount_and_advance: rollout={rollout_name} counts={counts}, requesting next batch")
	frappe.enqueue(
		"mazeed_custom_press.release_rollout.start_next_sites",
		rollout_name=rollout.name,
		queue="short",
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
	# Rows already Running drain rather than abort, and burst workers stop only
	# once they finish -- stopwaitsecs on the program is what keeps a cancel
	# from interrupting a migration mid-flight.
	_scale_burst_workers(rollout.name, start=False)
	logger.info(f"cancel_rollout: rollout={rollout_name} cancelled by user={frappe.session.user}")


def pause_rollout(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status != "Running":
		frappe.throw(_("Only a running rollout can be paused"))
	frappe.db.set_value("Release Rollout", rollout.name, "status", "Paused")
	logger.info(f"pause_rollout: rollout={rollout_name} paused by user={frappe.session.user}")


def resume_rollout(rollout_name: str):
	rollout = _lock_rollout(rollout_name)
	if rollout.status != "Paused":
		frappe.throw(_("Only a paused rollout can be resumed"))
	frappe.db.set_value("Release Rollout", rollout.name, "status", "Running")
	logger.info(f"resume_rollout: rollout={rollout_name} resumed by user={frappe.session.user}")
	# Reuses the normal advance path: recount, evaluate the canary gate,
	# finish if everything drained while paused, otherwise refill capacity.
	_recount_and_advance(rollout.name)


def reconcile_running_rollouts():
	running = frappe.get_all("Release Rollout", {"status": "Running"}, pluck="name")
	if running:
		logger.info(f"reconcile_running_rollouts: tick running_rollouts={running}")
	for rollout_name in running:
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
				logger.info(f"reconcile_running_rollouts: {row_name} stuck in Starting, found site_update={update}")
				frappe.db.set_value("Release Rollout Site", row_name, {"site_update": update, "status": "Running"})
				sync_site_update(update)
			else:
				logger.info(f"reconcile_running_rollouts: {row_name} stuck in Starting with no site_update, releasing to Pending")
				frappe.db.set_value("Release Rollout Site", row_name, "status", "Pending")
		frappe.db.set_value("Release Rollout", rollout_name, "last_reconciled_at", now_datetime())
		_recount_and_advance(rollout_name)


def _rollout_servers(rollout_name: str) -> list[str]:
	"""Servers holding sites this rollout still has to touch."""
	benches = frappe.get_all(
		"Release Rollout Site", {"rollout": rollout_name}, pluck="source_bench", distinct=True
	)
	if not benches:
		return []
	return frappe.get_all("Bench", {"name": ("in", benches)}, pluck="server", distinct=True)


def _scale_burst_workers(rollout_name: str, start: bool):
	"""Add or give back agent worker capacity for this rollout's servers.

	Agent workers are per server while max_concurrent_updates is per rollout,
	so the extra capacity is only worth anything alongside a concurrency limit
	above the default of 2 -- on its own it mostly stops backups and New Bench
	image pulls from blocking updates. Never fails the rollout: a server whose
	agent predates burst workers just logs and is skipped, since the rollout
	itself is still perfectly able to run at the old speed.
	"""
	from press.agent import Agent

	from mazeed_custom_press.agent_job_types import START_BURST_WORKERS, STOP_BURST_WORKERS

	# create_agent_job directly rather than a method on Agent: these job types
	# belong to this app, so their request paths live here too, instead of
	# needing a patch to press.agent.Agent.
	job_type = START_BURST_WORKERS if start else STOP_BURST_WORKERS
	for server in _rollout_servers(rollout_name):
		try:
			Agent(server).create_agent_job(
				job_type["name"],
				job_type["request_path"],
				reference_doctype="Release Rollout",
				reference_name=rollout_name,
			)
			logger.info(
				f"_scale_burst_workers: rollout={rollout_name} server={server} "
				f"action={'start' if start else 'stop'}"
			)
		except Exception as exc:
			logger.info(
				f"_scale_burst_workers: rollout={rollout_name} server={server} "
				f"action={'start' if start else 'stop'} failed (ignored): {exc}"
			)


def _lock_rollout(name: str):
	return frappe.get_doc("Release Rollout", name, for_update=True)


def _status_counts(rollout_name: str) -> dict[str, int]:
	rows = frappe.db.sql(
		"SELECT status, COUNT(*) AS count FROM `tabRelease Rollout Site` WHERE rollout=%s GROUP BY status",
		rollout_name,
		as_dict=True,
	)
	return {row.status: cint(row.count) for row in rows}


def _recount(rollout_name: str) -> dict[str, int]:
	"""Persist the display counters and return the counts computed.

	Callers that also need the counts for a decision (canary gate, completion
	check) should use this return value instead of calling _status_counts()
	again -- at large site counts that second aggregate query is pure waste,
	since nothing changes the site rows between the two calls.
	"""
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
	return counts
