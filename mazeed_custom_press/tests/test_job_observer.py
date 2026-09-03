"""Slice 5 — EVENT-01..08: completion observation and immediate refill."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import start_next_sites, start_rollout_site, sync_site_update
from mazeed_custom_press.tests.rollout_test_utils import (
	create_updateable_site_environment,
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	fabricate_site_update,
	make_rollout,
	make_rollout_site,
)


def make_running_row():
	"""A rollout row that is Running with a real, linked Site Update, plus one
	Pending row so a freed slot has a next site to start."""
	from press.press.doctype.agent_job.agent_job import AgentJob

	from mazeed_custom_press.tests.rollout_test_utils import fabricate_site

	environment = create_updateable_site_environment()
	rollout = make_rollout(environment.group.name, total_sites=2)
	row = make_rollout_site(
		rollout.name, environment.site.name, environment.bench1.name, status="Starting"
	)
	make_rollout_site(rollout.name, fabricate_site(environment.bench1.name), environment.bench1.name)
	with patch.object(AgentJob, "enqueue_http_request", new=Mock()):
		start_rollout_site(row.name)
	row.reload()
	assert row.status == "Running" and row.site_update
	return environment, rollout, row


def refill_calls(enqueue):
	return [
		called
		for called in enqueue.call_args_list
		if called.args and called.args[0].endswith("start_next_sites")
	]


class TestSiteUpdateSynchronization(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def assert_terminal_transition(self, site_update_status, expected_row_status):
		environment, rollout, row = make_running_row()
		frappe.db.set_value("Site Update", row.site_update, "status", site_update_status)

		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			sync_site_update(row.site_update)

		row.reload()
		self.assertEqual(row.status, expected_row_status)
		self.assertTrue(row.finished_at)
		self.assertTrue(refill_calls(enqueue), "terminal result must refill the queue")

	def test_event_01_success_marks_the_row_success_and_refills(self):
		self.assert_terminal_transition("Success", "Success")

	def test_event_02_recovered_marks_recovered_and_refills(self):
		self.assert_terminal_transition("Recovered", "Recovered")

	def test_event_03_fatal_marks_fatal_and_refills(self):
		self.assert_terminal_transition("Fatal", "Fatal")

	def test_event_04_cancelled_marks_cancelled_and_refills(self):
		self.assert_terminal_transition("Cancelled", "Cancelled")

	def test_event_05_failure_keeps_the_row_running_and_does_not_open_a_slot(self):
		environment, rollout, row = make_running_row()
		frappe.db.set_value("Site Update", row.site_update, "status", "Failure")

		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			sync_site_update(row.site_update)

		row.reload()
		self.assertEqual(row.status, "Running")
		self.assertFalse(row.finished_at)
		self.assertFalse(refill_calls(enqueue))

	def test_event_06_duplicate_terminal_observation_does_not_open_two_slots(self):
		environment, rollout, row = make_running_row()
		frappe.db.set_value("Site Update", row.site_update, "status", "Success")

		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			sync_site_update(row.site_update)
			first_refills = len(refill_calls(enqueue))
			sync_site_update(row.site_update)

		row.reload()
		self.assertEqual(row.status, "Success")
		self.assertEqual(len(refill_calls(enqueue)), first_refills)

	def test_event_07_unrelated_site_updates_and_agent_jobs_are_ignored(self):
		from mazeed_custom_press.release_rollout import observe_agent_job
		from mazeed_custom_press.tests.rollout_test_utils import fabricate, fabricate_bench, fabricate_release_group, fabricate_site

		group = fabricate_release_group()
		bench = fabricate_bench(group)
		site = fabricate_site(bench)
		unrelated_update = fabricate_site_update(site, status="Success")
		sync_site_update(unrelated_update)  # no rollout row: must be a no-op, not an error

		unrelated_job = fabricate("Agent Job", job_type="Backup Site", status="Success", site=site)
		job = frappe.get_doc("Agent Job", unrelated_job)
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			observe_agent_job(job)
		enqueue.assert_not_called()

	def test_event_08_the_agent_job_hook_observes_status_after_press_processing(self):
		environment, rollout, row = make_running_row()
		update_job = frappe.db.get_value("Site Update", row.site_update, "update_job")
		self.assertTrue(update_job)

		# Simulate Press finishing its processing: job terminal, Site Update terminal.
		frappe.db.set_value("Agent Job", update_job, "status", "Success", update_modified=False)
		frappe.db.set_value("Site Update", row.site_update, "status", "Success")

		job = frappe.get_doc("Agent Job", update_job)
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			job.run_method("on_change")  # fires doc_events, including our observer

		sync_calls = [
			called
			for called in enqueue.call_args_list
			if called.args and called.args[0].endswith("sync_site_update")
		]
		self.assertEqual(len(sync_calls), 1)
		self.assertEqual(sync_calls[0].kwargs.get("site_update_name"), row.site_update)
		# The sync is enqueued after commit, so it reads the status Press wrote,
		# regardless of hook ordering inside the request.
		self.assertTrue(sync_calls[0].kwargs.get("enqueue_after_commit"))

		sync_site_update(row.site_update)
		row.reload()
		self.assertEqual(row.status, "Success")

	def test_event_09_update_status_override_syncs_immediately_without_relying_on_the_dead_hook(self):
		# The Agent Job on_change hook above only fires because the test
		# manually calls run_method("on_change") -- real Press never does
		# that; it writes status via a raw db.set_value (see
		# press.press.doctype.site_update.site_update.update_status), which
		# never fires on_change at all. This is what actually detects
		# completion in production: patching that module-level function.
		from press.press.doctype.site_update import site_update as site_update_module

		from mazeed_custom_press.overrides.site_update import apply_overrides

		# apply_overrides() mutates this module-level function for the whole
		# process, not just this test -- without restoring it, every later
		# test in the same `bench run-tests` run (including the Slice 0
		# characterization tests, which assume vanilla Press behavior) would
		# silently run against our patched version instead.
		original_update_status = site_update_module.update_status
		self.addCleanup(setattr, site_update_module, "update_status", original_update_status)

		apply_overrides()
		environment, rollout, row = make_running_row()

		with patch("mazeed_custom_press.overrides.site_update.frappe.enqueue") as enqueue:
			site_update_module.update_status(row.site_update, "Success")

		self.assertEqual(frappe.db.get_value("Site Update", row.site_update, "status"), "Success")
		sync_calls = [
			called
			for called in enqueue.call_args_list
			if called.args and called.args[0].endswith("sync_site_update")
		]
		self.assertEqual(len(sync_calls), 1)
		self.assertEqual(sync_calls[0].kwargs.get("site_update_name"), row.site_update)
		self.assertEqual(sync_calls[0].kwargs.get("queue"), "short")
		self.assertTrue(sync_calls[0].kwargs.get("enqueue_after_commit"))

	def test_event_10_a_single_completion_refills_exactly_one_slot_keeping_concurrency_constant(self):
		# With 3 running and 2 pending, one running site finishing must start
		# exactly 1 new site (not 0, not both pending ones), leave the other 2
		# running sites untouched, and keep total active at the concurrency
		# cap of 3 -- rolling one-at-a-time refill, never "wait for the batch".
		from press.press.doctype.agent_job.agent_job import AgentJob
		from press.press.doctype.site.test_site import create_test_site

		environment = create_updateable_site_environment()
		rollout = make_rollout(environment.group.name, max_concurrent_updates=3, total_sites=5)

		sites = [environment.site.name] + [
			create_test_site(bench=environment.bench1.name).name for _ in range(4)
		]

		running_rows = []
		with patch.object(AgentJob, "enqueue_http_request", new=Mock()):
			for site in sites[:3]:
				row = make_rollout_site(rollout.name, site, environment.bench1.name, status="Starting")
				start_rollout_site(row.name)
				row.reload()
				self.assertEqual(row.status, "Running")
				running_rows.append(row)

		pending_rows = [
			make_rollout_site(rollout.name, site, environment.bench1.name) for site in sites[3:]
		]

		frappe.db.set_value("Site Update", running_rows[0].site_update, "status", "Success")
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue"):
			sync_site_update(running_rows[0].site_update)
		start_next_sites(rollout.name)

		for row in running_rows[1:] + pending_rows:
			row.reload()
		running_rows[0].reload()

		self.assertEqual(running_rows[0].status, "Success")  # the one that finished
		self.assertEqual(running_rows[1].status, "Running")  # untouched
		self.assertEqual(running_rows[2].status, "Running")  # untouched

		pending_statuses = [row.status for row in pending_rows]
		self.assertEqual(pending_statuses.count("Starting"), 1, "exactly one pending site must start")
		self.assertEqual(pending_statuses.count("Pending"), 1, "the other pending site must stay untouched")

		active = frappe.db.count(
			"Release Rollout Site", {"rollout": rollout.name, "status": ("in", ("Starting", "Running"))}
		)
		self.assertEqual(active, 3, "concurrency must be held constant, not exceeded or reduced")

	def test_event_11_adopts_a_site_update_press_already_scheduled_outside_this_rollout(self):
		# Press's own deploy flow (Bench Update -> update_sites_on_server) can call
		# site.schedule_update() for a site on its own once a new Bench goes Active,
		# independently of this rollout. That Site Update must be adopted, not raced
		# against -- and schedule_update() must never even be attempted for this site.
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		site = fabricate_site(bench)
		pre_existing_update = fabricate_site_update(site, status="Running")

		rollout = make_rollout(group)
		row = make_rollout_site(rollout.name, site, bench, status="Starting")

		from press.press.doctype.site.site import Site

		with patch.object(Site, "schedule_update") as schedule_update:
			start_rollout_site(row.name)
			schedule_update.assert_not_called()

		row.reload()
		self.assertEqual(row.status, "Running")
		self.assertEqual(row.site_update, pre_existing_update)

	def test_event_12_does_not_adopt_a_site_update_already_claimed_by_another_row(self):
		# A Site Update already tracked by a different Release Rollout Site row is
		# not up for grabs -- adoption must never let two rows point at the same
		# Site Update, no matter which rollout claimed it first.
		from press.press.doctype.agent_job.agent_job import AgentJob

		group = fabricate_release_group()
		bench = fabricate_bench(group)
		site = fabricate_site(bench)
		claimed_update = fabricate_site_update(site, status="Running")

		other_group = fabricate_release_group()
		other_rollout = make_rollout(other_group)
		other_bench = fabricate_bench(other_group)
		make_rollout_site(
			other_rollout.name, site, other_bench, status="Running", site_update=claimed_update
		)

		rollout = make_rollout(group)
		row = make_rollout_site(rollout.name, site, bench, status="Starting")

		with patch.object(AgentJob, "enqueue_http_request", new=Mock()):
			start_rollout_site(row.name)

		row.reload()
		self.assertNotEqual(row.site_update, claimed_update)
