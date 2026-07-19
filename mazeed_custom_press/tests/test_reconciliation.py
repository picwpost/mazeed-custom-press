"""Slice 6 — RECON-01..08: reconciliation and completion."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import reconcile_running_rollouts
from mazeed_custom_press.tests.rollout_test_utils import (
	backdate_modified,
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	fabricate_site_update,
	make_rollout,
	make_rollout_site,
)


def make_rollout_with_rows(row_specs, limit=2):
	"""row_specs: list of (row_status, site_update_status or None)."""
	group = fabricate_release_group()
	bench = fabricate_bench(group)
	rollout = make_rollout(group, max_concurrent_updates=limit, total_sites=len(row_specs))
	rows = []
	for row_status, update_status in row_specs:
		site = fabricate_site(bench)
		row = make_rollout_site(rollout.name, site, bench, status=row_status)
		if update_status is not None:
			update = fabricate_site_update(site, status=update_status, release_rollout_site=row.name)
			frappe.db.set_value("Release Rollout Site", row.name, "site_update", update, update_modified=False)
			row.reload()
		rows.append(row)
	return rollout, rows


@patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock())
class TestReconciliation(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_recon_01_a_missed_success_event_is_synchronized(self):
		rollout, rows = make_rollout_with_rows([("Running", "Success"), ("Pending", None)])
		reconcile_running_rollouts()
		rows[0].reload()
		self.assertEqual(rows[0].status, "Success")
		self.assertTrue(frappe.db.get_value("Release Rollout", rollout.name, "last_reconciled_at"))

	def test_recon_02_stale_starting_without_site_update_returns_to_pending(self):
		rollout, rows = make_rollout_with_rows([("Starting", None)])
		backdate_modified("Release Rollout Site", rows[0].name, minutes=30)
		reconcile_running_rollouts()
		rows[0].reload()
		self.assertEqual(rows[0].status, "Pending")

	def test_recon_02b_fresh_starting_rows_are_left_alone(self):
		rollout, rows = make_rollout_with_rows([("Starting", None)])
		reconcile_running_rollouts()
		rows[0].reload()
		self.assertEqual(rows[0].status, "Starting")

	def test_recon_03_stale_starting_with_site_update_relinks_without_creating_a_duplicate(self):
		rollout, rows = make_rollout_with_rows([("Starting", None)])
		row = rows[0]
		# The starter created a Site Update but crashed before saving the link.
		update = fabricate_site_update(row.site, status="Running", release_rollout_site=row.name)
		backdate_modified("Release Rollout Site", row.name, minutes=30)

		reconcile_running_rollouts()

		row.reload()
		self.assertEqual(row.status, "Running")
		self.assertEqual(row.site_update, update)
		self.assertEqual(
			frappe.db.count("Site Update", {"release_rollout_site": row.name}), 1
		)

	def test_recon_04_available_capacity_is_refilled(self):
		rollout, rows = make_rollout_with_rows([("Running", "Success"), ("Pending", None)])
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			reconcile_running_rollouts()
		refills = [
			called
			for called in enqueue.call_args_list
			if called.args and called.args[0].endswith("start_next_sites")
		]
		self.assertTrue(refills)

	def test_recon_05_all_success_rollout_becomes_completed(self):
		rollout, rows = make_rollout_with_rows([("Running", "Success"), ("Running", "Recovered")])
		reconcile_running_rollouts()
		rollout.reload()
		self.assertEqual(rollout.status, "Completed")
		self.assertEqual(rollout.stage, "Finished")
		self.assertTrue(rollout.finished_at)
		self.assertEqual(rollout.success_sites, 1)
		self.assertEqual(rollout.recovered_sites, 1)

	def test_recon_06_any_fatal_or_skipped_row_produces_completed_with_failures(self):
		rollout, rows = make_rollout_with_rows([("Running", "Success"), ("Running", "Fatal")])
		reconcile_running_rollouts()
		rollout.reload()
		self.assertEqual(rollout.status, "Completed With Failures")

		rollout_two, _ = make_rollout_with_rows([("Skipped", None), ("Running", "Success")])
		reconcile_running_rollouts()
		rollout_two.reload()
		self.assertEqual(rollout_two.status, "Completed With Failures")

	def test_recon_07_finished_at_is_set_once(self):
		rollout, rows = make_rollout_with_rows([("Running", "Success")])
		reconcile_running_rollouts()
		rollout.reload()
		first_finished_at = rollout.finished_at
		self.assertTrue(first_finished_at)

		reconcile_running_rollouts()  # completed rollouts are not reprocessed
		rollout.reload()
		self.assertEqual(rollout.finished_at, first_finished_at)
		self.assertEqual(rollout.status, "Completed")

	def test_recon_08_reconciliation_is_idempotent(self):
		rollout, rows = make_rollout_with_rows(
			[("Running", "Success"), ("Running", "Failure"), ("Pending", None)]
		)
		reconcile_running_rollouts()
		state_after_first = frappe.get_all(
			"Release Rollout Site",
			filters={"rollout": rollout.name},
			fields=["name", "status"],
			order_by="name",
		)
		reconcile_running_rollouts()
		state_after_second = frappe.get_all(
			"Release Rollout Site",
			filters={"rollout": rollout.name},
			fields=["name", "status"],
			order_by="name",
		)
		self.assertEqual(state_after_first, state_after_second)
		rollout.reload()
		self.assertEqual(rollout.status, "Running")  # Failure row is still in flight
