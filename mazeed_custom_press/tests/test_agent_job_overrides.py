"""Overrides this app applies to Press's Agent Job handling."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.agent_job_types import (
	BURST_WORKER_JOB_TYPES,
	MAINTENANCE_MODE_STEP,
	PURGE_PENDING_JOBS_STEP,
	SITE_UPDATE_JOB_TYPES,
	sync,
)
from mazeed_custom_press.overrides.agent_job import (
	MAX_RETRY_DELAY_IN_SECONDS,
	apply_overrides,
	custom_get_next_retry_at,
)


class TestRetryBackoffCap(FrappeTestCase):
	def delay_for(self, retry_count: int) -> int:
		before = frappe.utils.now_datetime()
		return round(
			frappe.utils.time_diff_in_seconds(custom_get_next_retry_at(retry_count), before)
		)

	def test_early_retries_are_unchanged_so_a_down_server_is_still_backed_off(self):
		self.assertEqual([self.delay_for(n) for n in (1, 2, 3)], [1, 32, 243])

	def test_later_retries_are_capped_instead_of_parking_a_job_for_hours(self):
		# Uncapped these were 1024s, 3125s and 7776s -- the last being the
		# default max_retry_count, so a job could sit undelivered for over two
		# hours after the server it targets had already recovered.
		self.assertEqual(
			[self.delay_for(n) for n in (4, 5, 6)], [MAX_RETRY_DELAY_IN_SECONDS] * 3
		)

	def test_total_wait_across_every_retry_stays_under_half_an_hour(self):
		total = sum(self.delay_for(n) for n in range(1, 7))
		self.assertLess(total, 30 * 60, "a job must not be undeliverable for longer than this")

	def test_patching_press_is_idempotent_across_repeated_worker_hooks(self):
		from press.press.doctype.agent_job import agent_job as agent_job_module

		apply_overrides()
		once = agent_job_module.get_next_retry_at
		apply_overrides()
		self.assertIs(agent_job_module.get_next_retry_at, once)
		self.assertIs(once, custom_get_next_retry_at)


class TestAgentJobTypeSync(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_burst_worker_job_types_exist_so_press_can_create_those_jobs(self):
		# Agent Job.after_insert loads the Agent Job Type to build its steps, so
		# a missing record makes the job raise instead of scaling workers.
		for job_type in BURST_WORKER_JOB_TYPES:
			self.assertTrue(frappe.db.exists("Agent Job Type", job_type["name"]))
			self.assertEqual(
				frappe.db.get_value("Agent Job Type", job_type["name"], "request_path"),
				job_type["request_path"],
			)

	def test_purge_step_sits_immediately_after_maintenance_mode(self):
		# Order must match what the agent runs, or the dashboard pairs step
		# names with the wrong results.
		for name in SITE_UPDATE_JOB_TYPES:
			steps = [s.step_name for s in frappe.get_doc("Agent Job Type", name).steps]
			self.assertEqual(
				steps.index(PURGE_PENDING_JOBS_STEP),
				steps.index(MAINTENANCE_MODE_STEP) + 1,
				f"{name} steps are {steps}",
			)

	def test_sync_is_idempotent_and_never_adds_the_step_twice(self):
		# after_migrate runs on every migrate, so this must be safe to repeat.
		sync()
		sync()
		for name in SITE_UPDATE_JOB_TYPES:
			steps = [s.step_name for s in frappe.get_doc("Agent Job Type", name).steps]
			self.assertEqual(steps.count(PURGE_PENDING_JOBS_STEP), 1, f"{name} steps are {steps}")

	def test_sync_reapplies_the_step_after_a_press_fixture_resync_drops_it(self):
		# "Agent Job Type" is in Press's fixtures hook, so every bench migrate
		# overwrites these documents from press/fixtures/agent_job_type.json --
		# which is exactly why this lives in after_migrate and not in that file.
		name = "Update Site Migrate"
		doc = frappe.get_doc("Agent Job Type", name)
		doc.steps = [s for s in doc.steps if s.step_name != PURGE_PENDING_JOBS_STEP]
		doc.save(ignore_permissions=True)
		self.assertNotIn(
			PURGE_PENDING_JOBS_STEP, [s.step_name for s in frappe.get_doc("Agent Job Type", name).steps]
		)

		sync()

		steps = [s.step_name for s in frappe.get_doc("Agent Job Type", name).steps]
		self.assertEqual(steps.index(PURGE_PENDING_JOBS_STEP), steps.index(MAINTENANCE_MODE_STEP) + 1)

	def test_step_is_skipped_with_a_logged_error_when_press_drops_maintenance_mode(self):
		# Guessing a position would mislabel every later step in the timeline.
		name = "Update Site Pull"
		doc = frappe.get_doc("Agent Job Type", name)
		doc.steps = [
			s for s in doc.steps if s.step_name not in (MAINTENANCE_MODE_STEP, PURGE_PENDING_JOBS_STEP)
		]
		doc.save(ignore_permissions=True)

		sync()

		steps = [s.step_name for s in frappe.get_doc("Agent Job Type", name).steps]
		self.assertNotIn(PURGE_PENDING_JOBS_STEP, steps)
