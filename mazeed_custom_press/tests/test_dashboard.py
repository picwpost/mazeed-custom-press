"""Slice 8 — DASH-01..10: read-only operations dashboard APIs."""

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.api.release_rollout import get_rollout_sites, get_rollout_summary
from mazeed_custom_press.release_rollout import _recount
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	make_rollout,
	make_rollout_site,
	make_website_user,
)

ROW_STATUSES = [
	("Pending", 0),
	("Starting", 0),
	("Running", 1),
	("Success", 1),
	("Recovered", 0),
	("Fatal", 0),
	("Skipped", 0),
	("Cancelled", 0),
]


def make_dashboard_rollout(limit=2):
	group = fabricate_release_group(team="team-owner")
	bench = fabricate_bench(group)
	rollout = make_rollout(
		group, max_concurrent_updates=limit, canary_size=2, stage="Main",
		canary_status="Passed", total_sites=len(ROW_STATUSES),
	)
	for status, is_canary in ROW_STATUSES:
		make_rollout_site(rollout.name, fabricate_site(bench), bench, status=status, is_canary=is_canary)
	_recount(rollout.name)
	return rollout


class TestDashboardApis(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_dash_01_summary_returns_every_required_counter_and_current_stage(self):
		rollout = make_dashboard_rollout()
		summary = get_rollout_summary(rollout.name)
		for key in (
			"status", "stage", "canary_status", "max_concurrent_updates", "total_sites",
			"pending_sites", "starting_sites", "running_sites", "success_sites",
			"recovered_sites", "failed_sites", "skipped_sites", "cancelled_sites",
			"completed_count", "updated_sites", "active_count", "progress_percent",
			"server_time", "started_by", "started_at",
		):
			self.assertIn(key, summary, key)
		self.assertEqual(summary.stage, "Main")
		self.assertEqual(summary.total_sites, 8)
		self.assertEqual(summary.pending_sites, 1)
		self.assertEqual(summary.starting_sites, 1)
		self.assertEqual(summary.running_sites, 1)

	def test_dash_02_updated_equals_success_plus_recovered(self):
		rollout = make_dashboard_rollout()
		summary = get_rollout_summary(rollout.name)
		self.assertEqual(summary.updated_sites, summary.success_sites + summary.recovered_sites)
		self.assertEqual(summary.updated_sites, 2)

	def test_dash_03_progress_uses_all_terminal_statuses(self):
		rollout = make_dashboard_rollout()
		summary = get_rollout_summary(rollout.name)
		# Success + Recovered + Fatal + Skipped + Cancelled = 5 of 8
		self.assertEqual(summary.completed_count, 5)
		self.assertAlmostEqual(summary.progress_percent, 5 / 8 * 100)

	def test_dash_04_recovered_and_fatal_remain_separately_visible(self):
		rollout = make_dashboard_rollout()
		summary = get_rollout_summary(rollout.name)
		self.assertEqual(summary.recovered_sites, 1)
		self.assertEqual(summary.failed_sites, 1)

	def test_dash_05_canary_status_is_reported_verbatim(self):
		rollout = make_dashboard_rollout()
		for canary_status in ("Pending", "Running", "Passed", "Failed"):
			frappe.db.set_value("Release Rollout", rollout.name, "canary_status", canary_status)
			self.assertEqual(get_rollout_summary(rollout.name).canary_status, canary_status)

	def test_dash_06_site_results_support_status_and_canary_filters(self):
		rollout = make_dashboard_rollout()
		success_rows = get_rollout_sites(rollout.name, status="Success")
		self.assertEqual({row.status for row in success_rows}, {"Success"})

		canary_rows = get_rollout_sites(rollout.name, stage="Canary")
		self.assertEqual(len(canary_rows), 2)
		self.assertTrue(all(row.is_canary for row in canary_rows))

		main_rows = get_rollout_sites(rollout.name, stage="Main")
		self.assertEqual(len(main_rows), 6)
		self.assertTrue(all(not row.is_canary for row in main_rows))

	def test_dash_07_site_results_are_paginated(self):
		rollout = make_dashboard_rollout()
		first_page = get_rollout_sites(rollout.name, start=0, page_length=3)
		second_page = get_rollout_sites(rollout.name, start=3, page_length=3)
		self.assertEqual(len(first_page), 3)
		self.assertEqual(len(second_page), 3)
		self.assertFalse({row.name for row in first_page} & {row.name for row in second_page})
		# Page length is capped server-side so a poll can never fetch unbounded rows.
		capped = get_rollout_sites(rollout.name, page_length=100000)
		self.assertLessEqual(len(capped), 100)

	def test_dash_07b_active_and_failed_rows_sort_before_completed_successes(self):
		rollout = make_dashboard_rollout()
		statuses = [row.status for row in get_rollout_sites(rollout.name)]
		self.assertLess(statuses.index("Running"), statuses.index("Success"))
		self.assertLess(statuses.index("Fatal"), statuses.index("Success"))

	def test_dash_08_users_without_release_group_access_cannot_read_the_rollout(self):
		rollout = make_dashboard_rollout()
		outsider = make_website_user()
		frappe.set_user(outsider)
		with self.assertRaises(frappe.PermissionError):
			get_rollout_summary(rollout.name)
		with self.assertRaises(frappe.PermissionError):
			get_rollout_sites(rollout.name)

	def test_dash_09_summary_signals_a_final_state_so_polling_can_stop(self):
		rollout = make_dashboard_rollout()
		frappe.db.set_value("Release Rollout", rollout.name, "status", "Completed With Failures")
		summary = get_rollout_summary(rollout.name)
		self.assertNotEqual(summary.status, "Running")

	def test_dash_10_displayed_active_count_never_exceeds_the_concurrency_limit(self):
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		rollout = make_rollout(group, max_concurrent_updates=2, total_sites=4)
		for _ in range(4):  # more active rows than the limit should ever allow
			make_rollout_site(rollout.name, fabricate_site(bench), bench, status="Running")
		_recount(rollout.name)

		summary = get_rollout_summary(rollout.name)
		self.assertEqual(summary.running_sites, 4)  # raw truth stays visible
		self.assertLessEqual(summary.active_count, summary.max_concurrent_updates)
