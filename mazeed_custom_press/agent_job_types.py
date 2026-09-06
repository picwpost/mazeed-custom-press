"""Agent Job Type records this app needs, kept out of Press's own fixture file.

"Agent Job Type" is listed in Press's `fixtures` hook, so every `bench migrate`
re-imports `press/fixtures/agent_job_type.json` and overwrites each job type it
defines. Editing that file to add our step would work until the next time Press
is pulled, and would be lost silently. Re-applying from `after_migrate` instead
-- which frappe runs after `sync_fixtures()` -- survives both a Press upgrade
and a fixture re-sync.
"""

from __future__ import annotations

import frappe

# New job types Press knows nothing about. Safe to insert once: fixture sync
# only overwrites the documents its own JSON names, and never deletes others.
START_BURST_WORKERS = {"name": "Start Burst Workers", "request_path": "server/start-burst-workers"}
STOP_BURST_WORKERS = {"name": "Stop Burst Workers", "request_path": "server/stop-burst-workers"}
BURST_WORKER_JOB_TYPES = (START_BURST_WORKERS, STOP_BURST_WORKERS)

# Steps Press's own job types must gain. These documents DO come from Press's
# fixture file, so this has to run on every migrate rather than once.
PURGE_PENDING_JOBS_STEP = "Purge Pending Jobs"
MAINTENANCE_MODE_STEP = "Enable Maintenance Mode"
SITE_UPDATE_JOB_TYPES = ("Update Site Pull", "Update Site Migrate")


def sync():
	create_burst_worker_job_types()
	add_purge_step_to_site_update_job_types()


def create_burst_worker_job_types():
	for job_type in BURST_WORKER_JOB_TYPES:
		if frappe.db.exists("Agent Job Type", job_type["name"]):
			continue
		frappe.get_doc(
			{
				"doctype": "Agent Job Type",
				"name": job_type["name"],
				"request_method": "POST",
				"request_path": job_type["request_path"],
				# Starting or stopping worker capacity is not worth retrying on
				# a schedule: a rollout that could not scale up just runs at the
				# old speed, and a retry storm against a struggling agent is the
				# last thing a deploy window needs.
				"disabled_auto_retry": 1,
				"max_retry_count": 3,
				"steps": [{"step_name": job_type["name"]}],
			}
		).insert(ignore_permissions=True)


def add_purge_step_to_site_update_job_types():
	"""Put `Purge Pending Jobs` right after `Enable Maintenance Mode`.

	Order has to match what the agent actually runs, or the dashboard timeline
	pairs step names with the wrong results. Maintenance mode comes first
	because it is what stops the site's queue refilling behind the purge.
	"""
	for name in SITE_UPDATE_JOB_TYPES:
		if not frappe.db.exists("Agent Job Type", name):
			continue
		doc = frappe.get_doc("Agent Job Type", name)
		step_names = [step.step_name for step in doc.steps]
		if PURGE_PENDING_JOBS_STEP in step_names:
			continue
		if MAINTENANCE_MODE_STEP not in step_names:
			# Press reordered this job type out from under us. Adding the step
			# at a guessed position would mislabel every later step, so skip and
			# say so rather than corrupt the timeline.
			frappe.log_error(
				title=f"Could not add {PURGE_PENDING_JOBS_STEP} to {name}",
				message=(
					f"{MAINTENANCE_MODE_STEP} is no longer a step of {name}; "
					f"steps are {step_names}. The agent still purges, but the "
					f"dashboard will not show the step."
				),
			)
			continue

		doc.append("steps", {"step_name": PURGE_PENDING_JOBS_STEP})
		# append() puts it last; move it to just after maintenance mode.
		doc.steps.insert(step_names.index(MAINTENANCE_MODE_STEP) + 1, doc.steps.pop())
		for index, step in enumerate(doc.steps, start=1):
			step.idx = index
		doc.save(ignore_permissions=True)
