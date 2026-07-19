"""Slice 5 — EVENT-01..08: completion observation and immediate refill."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import start_rollout_site, sync_site_update
from mazeed_custom_press.tests.rollout_test_utils import (
	create_updateable_site_environment,
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
