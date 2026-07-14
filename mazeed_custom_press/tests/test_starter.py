"""Slice 4 — START-01..07: starting a site safely."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import start_rollout_site
from mazeed_custom_press.tests.rollout_test_utils import (
	create_updateable_site_environment,
	make_rollout,
	make_rollout_site,
)


def make_claimed_row(environment):
	rollout = make_rollout(environment.group.name, total_sites=1)
	row = make_rollout_site(
		rollout.name, environment.site.name, environment.bench1.name, status="Starting"
	)
	return rollout, row


def mock_agent():
	from press.press.doctype.agent_job.agent_job import AgentJob

	return patch.object(AgentJob, "enqueue_http_request", new=Mock())


def site_updates_for(row_name):
	return frappe.get_all("Site Update", {"release_rollout_site": row_name}, pluck="name")


class TestStartRolloutSite(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_start_01_and_02_a_claimed_row_creates_one_linked_site_update_and_becomes_running(self):
		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)

		with mock_agent():
			start_rollout_site(row.name)

		row.reload()
		self.assertEqual(row.status, "Running")
		self.assertTrue(row.started_at)
		updates = site_updates_for(row.name)
		self.assertEqual(len(updates), 1)
		self.assertEqual(row.site_update, updates[0])

	def test_start_03_retrying_the_same_starter_does_not_create_a_second_site_update(self):
		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)

		with mock_agent():
			start_rollout_site(row.name)
			# Simulate a crashed starter retried after the Site Update was inserted.
			frappe.db.set_value(
				"Release Rollout Site", row.name, {"status": "Starting", "site_update": None}
			)
			start_rollout_site(row.name)

		row.reload()
		self.assertEqual(row.status, "Running")
		updates = site_updates_for(row.name)
		self.assertEqual(len(updates), 1)
		self.assertEqual(row.site_update, updates[0])

	def test_start_04_an_ineligible_site_becomes_skipped_and_frees_its_slot(self):
		from mazeed_custom_press.tests.rollout_test_utils import fabricate_site

		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)
		# A second pending row so the freed slot has somewhere to go.
		make_rollout_site(rollout.name, fabricate_site(environment.bench1.name), environment.bench1.name)
		frappe.db.set_value("Site", environment.site.name, "status", "Archived")

		with mock_agent(), patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			start_rollout_site(row.name)

		row.reload()
		self.assertEqual(row.status, "Skipped")
		self.assertIn("no longer eligible", row.last_error)
		self.assertEqual(site_updates_for(row.name), [])
		refills = [
			called
			for called in enqueue.call_args_list
			if called.args and called.args[0].endswith("start_next_sites")
		]
		self.assertTrue(refills)

	def test_start_05_a_site_moved_to_another_bench_becomes_skipped(self):
		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)
		frappe.db.set_value("Site", environment.site.name, "bench", environment.bench2.name)

		with mock_agent(), patch("mazeed_custom_press.release_rollout.frappe.enqueue"):
			start_rollout_site(row.name)

		row.reload()
		self.assertEqual(row.status, "Skipped")
		self.assertEqual(site_updates_for(row.name), [])

	def test_start_06_a_schedule_update_validation_error_is_recorded_safely(self):
		from press.press.doctype.site.site import Site

		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)

		with (
			patch.object(Site, "schedule_update", side_effect=frappe.ValidationError("update blocked")),
			patch("mazeed_custom_press.release_rollout.frappe.enqueue"),
		):
			start_rollout_site(row.name)

		row.reload()
		self.assertEqual(row.status, "Skipped")
		self.assertIn("update blocked", row.last_error)
		self.assertTrue(row.finished_at)

	def test_start_07_unexpected_failure_cannot_strand_starting_forever(self):
		from press.press.doctype.site.site import Site

		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)

		with (
			patch.object(Site, "schedule_update", side_effect=RuntimeError("agent exploded")),
			patch("mazeed_custom_press.release_rollout.frappe.enqueue"),
		):
			start_rollout_site(row.name)

		row.reload()
		self.assertNotEqual(row.status, "Starting")
		self.assertEqual(row.status, "Skipped")
		self.assertIn("agent exploded", row.last_error)

	def test_starter_only_acts_on_starting_rows(self):
		environment = create_updateable_site_environment()
		rollout, row = make_claimed_row(environment)
		frappe.db.set_value("Release Rollout Site", row.name, "status", "Pending")

		with mock_agent():
			start_rollout_site(row.name)

		row.reload()
		self.assertEqual(row.status, "Pending")
		self.assertEqual(site_updates_for(row.name), [])
