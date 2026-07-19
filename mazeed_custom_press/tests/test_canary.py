"""Slice 7 — CANARY-01..09: the canary gate.

Note on CANARY-07: the spec says main rows stay Pending after a failed
canary; the implementation marks them Skipped with an explanatory
last_error so the rollout reaches a final auditable state. That deviation
is tracked as a product decision (Jira FS-3042). These tests pin the
implemented behavior.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import (
	_recount_and_advance,
	create_release_rollout,
	start_next_sites,
	sync_site_update,
)
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	fabricate_site_update,
)


def make_canary_rollout(total_sites=5, canary_size=2, limit=3):
	group = fabricate_release_group()
	bench = fabricate_bench(group)
	for _ in range(total_sites):
		fabricate_site(bench)
	with patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock()):
		result = create_release_rollout(
			group, max_concurrent_updates=limit, canary_size=canary_size
		)
	return frappe.get_doc("Release Rollout", result["rollout"])


def rows_by_stage(rollout_name, is_canary):
	return frappe.get_all(
		"Release Rollout Site",
		filters={"rollout": rollout_name, "is_canary": is_canary},
		fields=["name", "status"],
	)


def finish_rows(rows, status):
	for row in rows:
		frappe.db.set_value("Release Rollout Site", row.name, "status", status, update_modified=False)


@patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock())
class TestCanaryGate(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_canary_selection_is_deterministic_by_site_name(self):
		rollout = make_canary_rollout(total_sites=4, canary_size=2)
		canaries = sorted(
			frappe.get_all(
				"Release Rollout Site", {"rollout": rollout.name, "is_canary": 1}, pluck="site"
			)
		)
		all_sites = sorted(
			frappe.get_all("Release Rollout Site", {"rollout": rollout.name}, pluck="site")
		)
		self.assertEqual(canaries, all_sites[:2])

	def test_canary_01_only_canary_rows_start_during_the_canary_stage(self):
		rollout = make_canary_rollout(total_sites=5, canary_size=2, limit=3)
		start_next_sites(rollout.name)
		canary_statuses = {row.status for row in rows_by_stage(rollout.name, 1)}
		main_statuses = {row.status for row in rows_by_stage(rollout.name, 0)}
		self.assertEqual(canary_statuses, {"Starting"})
		self.assertEqual(main_statuses, {"Pending"})
		rollout.reload()
		self.assertEqual(rollout.canary_status, "Running")
		self.assertTrue(rollout.canary_started_at)

	def test_canary_02_main_rows_remain_pending_until_every_canary_is_terminal(self):
		rollout = make_canary_rollout()
		start_next_sites(rollout.name)
		canaries = rows_by_stage(rollout.name, 1)
		finish_rows(canaries[:1], "Success")  # one canary done, one still Starting

		_recount_and_advance(rollout.name)

		rollout.reload()
		self.assertEqual(rollout.stage, "Canary")
		self.assertEqual({row.status for row in rows_by_stage(rollout.name, 0)}, {"Pending"})

	def test_canary_03_all_successful_canaries_promote_the_rollout_once(self):
		rollout = make_canary_rollout()
		start_next_sites(rollout.name)
		finish_rows(rows_by_stage(rollout.name, 1), "Success")

		_recount_and_advance(rollout.name)
		rollout.reload()
		self.assertEqual(rollout.stage, "Main")
		self.assertEqual(rollout.canary_status, "Passed")
		first_finish = rollout.canary_finished_at
		self.assertTrue(first_finish)

		_recount_and_advance(rollout.name)
		rollout.reload()
		self.assertEqual(rollout.canary_finished_at, first_finish)
		self.assertEqual(rollout.canary_status, "Passed")

	def test_canary_03b_recovered_counts_as_a_successful_canary(self):
		rollout = make_canary_rollout()
		start_next_sites(rollout.name)
		canaries = rows_by_stage(rollout.name, 1)
		finish_rows(canaries[:1], "Success")
		finish_rows(canaries[1:], "Recovered")

		_recount_and_advance(rollout.name)
		rollout.reload()
		self.assertEqual(rollout.canary_status, "Passed")
		self.assertEqual(rollout.stage, "Main")

	def test_canary_04_promotion_fills_main_stage_capacity(self):
		rollout = make_canary_rollout(total_sites=6, canary_size=2, limit=3)
		start_next_sites(rollout.name)
		finish_rows(rows_by_stage(rollout.name, 1), "Success")
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue") as enqueue:
			_recount_and_advance(rollout.name)
		refills = [
			called
			for called in enqueue.call_args_list
			if called.args and called.args[0].endswith("start_next_sites")
		]
		self.assertTrue(refills, "promotion must enqueue the controller")

		start_next_sites(rollout.name)
		main_rows = rows_by_stage(rollout.name, 0)
		starting = [row for row in main_rows if row.status == "Starting"]
		self.assertEqual(len(starting), 3)

	def test_canary_05_a_fatal_skipped_or_cancelled_canary_fails_the_gate(self):
		for failing_status in ("Fatal", "Skipped", "Cancelled"):
			rollout = make_canary_rollout()
			start_next_sites(rollout.name)
			canaries = rows_by_stage(rollout.name, 1)
			finish_rows(canaries[:1], "Success")
			finish_rows(canaries[1:], failing_status)

			_recount_and_advance(rollout.name)

			rollout.reload()
			self.assertEqual(rollout.canary_status, "Failed", failing_status)
			self.assertEqual(rollout.status, "Completed With Failures", failing_status)
			self.assertEqual(rollout.stage, "Finished", failing_status)

	def test_canary_06_failure_does_not_pass_or_fail_while_recovery_may_run(self):
		rollout = make_canary_rollout()
		start_next_sites(rollout.name)
		canaries = rows_by_stage(rollout.name, 1)
		site = frappe.db.get_value("Release Rollout Site", canaries[0].name, "site")
		update = fabricate_site_update(site, status="Failure", release_rollout_site=canaries[0].name)
		frappe.db.set_value("Release Rollout Site", canaries[0].name, {"status": "Running", "site_update": update})

		sync_site_update(update)

		rollout.reload()
		self.assertEqual(rollout.stage, "Canary")
		self.assertNotIn(rollout.canary_status, ("Passed", "Failed"))
		self.assertEqual(
			frappe.db.get_value("Release Rollout Site", canaries[0].name, "status"), "Running"
		)

	def test_canary_07_failed_canary_starts_no_main_rows(self):
		rollout = make_canary_rollout()
		start_next_sites(rollout.name)
		finish_rows(rows_by_stage(rollout.name, 1), "Fatal")

		_recount_and_advance(rollout.name)
		start_next_sites(rollout.name)  # a straggler controller job must be a no-op

		main_rows = rows_by_stage(rollout.name, 0)
		self.assertFalse([row for row in main_rows if row.status in ("Starting", "Running")])
		# Implemented behavior (spec deviation, see module docstring): rows are
		# Skipped and labeled so the dashboard explains why they never started.
		self.assertEqual({row.status for row in main_rows}, {"Skipped"})
		labels = frappe.get_all(
			"Release Rollout Site",
			{"rollout": rollout.name, "is_canary": 0},
			pluck="last_error",
		)
		self.assertTrue(all("canary" in (label or "") for label in labels))

	def test_canary_08_duplicate_terminal_observations_cannot_promote_twice_or_exceed_capacity(self):
		rollout = make_canary_rollout(total_sites=6, canary_size=2, limit=2)
		start_next_sites(rollout.name)
		finish_rows(rows_by_stage(rollout.name, 1), "Success")

		_recount_and_advance(rollout.name)
		_recount_and_advance(rollout.name)
		start_next_sites(rollout.name)
		start_next_sites(rollout.name)

		main_rows = rows_by_stage(rollout.name, 0)
		active = [row for row in main_rows if row.status in ("Starting", "Running")]
		self.assertEqual(len(active), 2)
		rollout.reload()
		self.assertEqual(rollout.canary_status, "Passed")

	def test_canary_09_canary_size_zero_starts_the_main_stage_directly(self):
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		for _ in range(3):
			fabricate_site(bench)
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock()):
			result = create_release_rollout(group, canary_size=0)

		rollout = frappe.get_doc("Release Rollout", result["rollout"])
		self.assertEqual(rollout.stage, "Main")
		self.assertEqual(rollout.canary_status, "Passed")

		start_next_sites(rollout.name)
		claimed = frappe.db.count(
			"Release Rollout Site", {"rollout": rollout.name, "status": "Starting"}
		)
		self.assertEqual(claimed, 2)
