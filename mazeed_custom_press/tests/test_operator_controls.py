"""Phase 2 slice — operator controls: cancel, pause, resume."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import (
	cancel_rollout,
	pause_rollout,
	resume_rollout,
	start_next_sites,
	start_rollout_site,
	sync_site_update,
)
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	fabricate_site_update,
	make_rollout,
	make_rollout_site,
)


def make_active_rollout(row_statuses=("Pending", "Pending"), limit=2):
	group = fabricate_release_group()
	bench = fabricate_bench(group)
	rollout = make_rollout(group, max_concurrent_updates=limit, total_sites=len(row_statuses))
	rows = [
		make_rollout_site(rollout.name, fabricate_site(bench), bench, status=status)
		for status in row_statuses
	]
	return rollout, rows, bench


def statuses(rollout_name):
	return frappe.get_all("Release Rollout Site", {"rollout": rollout_name}, pluck="status")


@patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock())
class TestCancelRollout(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_cancel_stops_pending_and_starting_rows_and_finalizes_the_rollout(self):
		rollout, rows, _ = make_active_rollout(("Pending", "Starting", "Running"))
		cancel_rollout(rollout.name)

		rollout.reload()
		self.assertEqual(rollout.status, "Cancelled")
		self.assertEqual(rollout.stage, "Finished")
		self.assertTrue(rollout.finished_at)
		self.assertEqual(sorted(statuses(rollout.name)), ["Cancelled", "Cancelled", "Running"])
		labels = frappe.get_all(
			"Release Rollout Site",
			{"rollout": rollout.name, "status": "Cancelled"},
			pluck="last_error",
		)
		self.assertTrue(all("Cancelled by operator" in label for label in labels))

	def test_cancel_leaves_in_flight_sites_to_drain_and_still_records_their_result(self):
		rollout, rows, _ = make_active_rollout(("Running", "Pending"))
		update = fabricate_site_update(rows[0].site, status="Pending", release_rollout_site=rows[0].name)
		frappe.db.set_value("Release Rollout Site", rows[0].name, "site_update", update)

		cancel_rollout(rollout.name)
		frappe.db.set_value("Site Update", update, "status", "Success")
		sync_site_update(update)

		rows[0].reload()
		rollout.reload()
		self.assertEqual(rows[0].status, "Success")
		self.assertEqual(rollout.status, "Cancelled")  # a late result cannot reopen it
		self.assertEqual(rollout.success_sites, 1)  # ...but the counters stay honest

	def test_controller_starts_nothing_after_cancel(self):
		rollout, rows, _ = make_active_rollout(("Pending", "Pending"))
		cancel_rollout(rollout.name)
		start_next_sites(rollout.name)
		self.assertNotIn("Starting", statuses(rollout.name))

	def test_cancel_requires_an_active_rollout(self):
		rollout, _, _ = make_active_rollout(("Pending",))
		cancel_rollout(rollout.name)
		with self.assertRaises(frappe.ValidationError) as context:
			cancel_rollout(rollout.name)
		self.assertIn("running or paused", str(context.exception))

	def test_a_paused_rollout_can_be_cancelled(self):
		rollout, _, _ = make_active_rollout(("Pending",))
		pause_rollout(rollout.name)
		cancel_rollout(rollout.name)
		rollout.reload()
		self.assertEqual(rollout.status, "Cancelled")


@patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock())
class TestPauseAndResume(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_pause_stops_new_claims(self):
		rollout, _, _ = make_active_rollout(("Pending", "Pending"))
		pause_rollout(rollout.name)
		start_next_sites(rollout.name)
		self.assertEqual(statuses(rollout.name), ["Pending", "Pending"])

	def test_a_claimed_starter_releases_its_claim_while_paused(self):
		rollout, rows, _ = make_active_rollout(("Starting",))
		pause_rollout(rollout.name)
		start_rollout_site(rows[0].name)
		rows[0].reload()
		self.assertEqual(rows[0].status, "Pending")
		self.assertFalse(rows[0].site_update)

	def test_in_flight_results_are_recorded_while_paused_without_refilling(self):
		rollout, rows, _ = make_active_rollout(("Running", "Pending"))
		update = fabricate_site_update(rows[0].site, status="Success", release_rollout_site=rows[0].name)
		frappe.db.set_value("Release Rollout Site", rows[0].name, "site_update", update)
		pause_rollout(rollout.name)

		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			sync_site_update(update)

		rows[0].reload()
		self.assertEqual(rows[0].status, "Success")
		self.assertEqual(statuses(rollout.name).count("Pending"), 1)
		enqueue.assert_not_called()

	def test_resume_refills_capacity(self):
		rollout, _, _ = make_active_rollout(("Pending", "Pending", "Pending"), limit=2)
		pause_rollout(rollout.name)
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			resume_rollout(rollout.name)
		refills = [
			called
			for called in enqueue.call_args_list
			if called.args and called.args[0].endswith("start_next_sites")
		]
		self.assertTrue(refills)

		start_next_sites(rollout.name)
		self.assertEqual(statuses(rollout.name).count("Starting"), 2)

	def test_resume_finishes_a_rollout_that_drained_while_paused(self):
		rollout, rows, _ = make_active_rollout(("Running", "Success"))
		update = fabricate_site_update(rows[0].site, status="Success", release_rollout_site=rows[0].name)
		frappe.db.set_value("Release Rollout Site", rows[0].name, "site_update", update)
		pause_rollout(rollout.name)
		frappe.db.set_value("Site Update", update, "status", "Success")
		sync_site_update(update)

		resume_rollout(rollout.name)

		rollout.reload()
		self.assertEqual(rollout.status, "Completed")
		self.assertTrue(rollout.finished_at)

	def test_pause_and_resume_guard_their_source_states(self):
		rollout, _, _ = make_active_rollout(("Pending",))
		with self.assertRaises(frappe.ValidationError) as context:
			resume_rollout(rollout.name)
		self.assertIn("paused rollout", str(context.exception))

		pause_rollout(rollout.name)
		with self.assertRaises(frappe.ValidationError) as context:
			pause_rollout(rollout.name)
		self.assertIn("running rollout", str(context.exception))


class TestOperatorApiPermissions(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_operator_actions_enforce_release_group_access(self):
		from mazeed_custom_press.api import release_rollout as api
		from mazeed_custom_press.tests.rollout_test_utils import make_website_user

		group = fabricate_release_group(team="team-of-someone-else")
		bench = fabricate_bench(group)
		rollout = make_rollout(group, total_sites=1)
		make_rollout_site(rollout.name, fabricate_site(bench), bench)

		outsider = make_website_user()
		frappe.set_user(outsider)
		for action in (api.cancel_rollout, api.pause_rollout, api.resume_rollout):
			with self.assertRaises(frappe.PermissionError):
				action(rollout.name)
