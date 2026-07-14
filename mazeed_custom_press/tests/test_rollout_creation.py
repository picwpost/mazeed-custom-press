"""Slice 2 — DATA-01..04 and SNAP-01..06: data model and snapshot creation."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import create_release_rollout
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	make_rollout,
	make_rollout_site,
)


def make_group_with_sites(site_statuses=("Active",)):
	group = fabricate_release_group()
	bench = fabricate_bench(group)
	sites = [fabricate_site(bench, status=status) for status in site_statuses]
	return group, bench, sites


@patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock())
class TestRolloutCreation(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_data_01_concurrency_must_be_greater_than_zero(self):
		group, _, _ = make_group_with_sites()
		with self.assertRaises(frappe.ValidationError):
			create_release_rollout(group, max_concurrent_updates=0)
		with self.assertRaises(frappe.ValidationError):
			create_release_rollout(group, max_concurrent_updates=-3)

	def test_data_02_a_rollout_cannot_contain_the_same_site_twice(self):
		group, bench, sites = make_group_with_sites()
		rollout = make_rollout(group)
		make_rollout_site(rollout.name, sites[0], bench)
		with self.assertRaises(frappe.UniqueValidationError):
			make_rollout_site(rollout.name, sites[0], bench)

	def test_data_03_a_second_active_rollout_for_a_release_group_is_rejected(self):
		group, _, _ = make_group_with_sites()
		for blocking_status in ("Draft", "Running"):
			rollout = make_rollout(group, status=blocking_status)
			with self.assertRaises(frappe.ValidationError) as context:
				create_release_rollout(group)
			self.assertIn("active rollout already exists", str(context.exception))
			frappe.delete_doc("Release Rollout", rollout.name, force=True)

	def test_data_04_completed_historical_rollouts_do_not_block_a_new_rollout(self):
		group, _, _ = make_group_with_sites()
		make_rollout(group, status="Completed")
		make_rollout(group, status="Completed With Failures")
		result = create_release_rollout(group)
		self.assertTrue(frappe.db.exists("Release Rollout", result["rollout"]))

	def test_snap_01_only_sites_on_active_benches_are_selected(self):
		group = fabricate_release_group()
		active_bench = fabricate_bench(group, status="Active")
		archived_bench = fabricate_bench(group, status="Archived")
		selected_site = fabricate_site(active_bench)
		fabricate_site(archived_bench)

		result = create_release_rollout(group)

		self.assertEqual(result["selected_sites"], 1)
		rows = frappe.get_all("Release Rollout Site", {"rollout": result["rollout"]}, pluck="site")
		self.assertEqual(rows, [selected_site])

	def test_snap_02_and_03_only_active_inactive_and_suspended_sites_are_selected(self):
		group, bench, _ = make_group_with_sites(
			("Active", "Inactive", "Suspended", "Archived", "Broken", "Pending")
		)
		result = create_release_rollout(group)
		self.assertEqual(result["selected_sites"], 3)
		selected_statuses = frappe.get_all(
			"Release Rollout Site", {"rollout": result["rollout"]}, pluck="site"
		)
		statuses = {frappe.db.get_value("Site", site, "status") for site in selected_statuses}
		self.assertEqual(statuses, {"Active", "Inactive", "Suspended"})

	def test_snap_04_source_bench_is_captured_on_every_row(self):
		group = fabricate_release_group()
		bench_a = fabricate_bench(group)
		bench_b = fabricate_bench(group)
		site_a = fabricate_site(bench_a)
		site_b = fabricate_site(bench_b)

		result = create_release_rollout(group)

		rows = frappe.get_all(
			"Release Rollout Site",
			filters={"rollout": result["rollout"]},
			fields=["site", "source_bench"],
		)
		by_site = {row.site: row.source_bench for row in rows}
		self.assertEqual(by_site[site_a], bench_a)
		self.assertEqual(by_site[site_b], bench_b)

	def test_snap_05_empty_selection_creates_no_rollout(self):
		group = fabricate_release_group()
		fabricate_bench(group, status="Archived")
		with self.assertRaises(frappe.ValidationError) as context:
			create_release_rollout(group)
		self.assertIn("No eligible sites", str(context.exception))
		self.assertFalse(frappe.db.exists("Release Rollout", {"release_group": group}))

	def test_snap_06_creation_never_commits_and_an_insert_failure_rolls_back_everything(self):
		from mazeed_custom_press.mazeed_custom_press.doctype.release_rollout_site.release_rollout_site import (
			ReleaseRolloutSite,
		)

		group, _, _ = make_group_with_sites(("Active", "Active", "Active"))
		failing_insert = Mock(side_effect=[None, None, frappe.ValidationError("third row broke")])
		with (
			patch.object(frappe.db, "commit") as commit,
			patch.object(ReleaseRolloutSite, "before_insert", failing_insert, create=True),
			self.assertRaises(frappe.ValidationError),
		):
			create_release_rollout(group)

		# No commit inside creation means the failed transaction is atomic: the
		# request-level rollback discards the parent and every inserted row.
		commit.assert_not_called()
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists("Release Rollout", {"release_group": group}))

	def test_press_settings_provide_the_default_limit_and_canary_size(self):
		group, _, _ = make_group_with_sites(("Active", "Active", "Active", "Active", "Active"))
		frappe.db.set_single_value("Press Settings", "rollout_max_concurrent_updates", 4)
		frappe.db.set_single_value("Press Settings", "rollout_canary_size", 3)

		result = create_release_rollout(group)

		rollout = frappe.get_doc("Release Rollout", result["rollout"])
		self.assertEqual(rollout.max_concurrent_updates, 4)
		self.assertEqual(rollout.canary_size, 3)

		# Changing settings must not touch the captured values of this rollout.
		frappe.db.set_single_value("Press Settings", "rollout_max_concurrent_updates", 9)
		rollout.reload()
		self.assertEqual(rollout.max_concurrent_updates, 4)

	def test_explicit_arguments_override_the_settings_defaults(self):
		group, _, _ = make_group_with_sites(("Active", "Active", "Active"))
		frappe.db.set_single_value("Press Settings", "rollout_max_concurrent_updates", 4)
		result = create_release_rollout(group, max_concurrent_updates=1, canary_size=0)
		rollout = frappe.get_doc("Release Rollout", result["rollout"])
		self.assertEqual(rollout.max_concurrent_updates, 1)
		self.assertEqual(rollout.canary_size, 0)
		self.assertEqual(rollout.stage, "Main")

	def test_rollout_snapshot_sets_counters_and_ownership(self):
		group, bench, sites = make_group_with_sites(("Active", "Active"))
		result = create_release_rollout(group)
		rollout = frappe.get_doc("Release Rollout", result["rollout"])
		self.assertEqual(rollout.status, "Running")
		self.assertEqual(rollout.total_sites, 2)
		self.assertEqual(rollout.pending_sites, 2)
		self.assertEqual(rollout.started_by, "Administrator")
		self.assertTrue(rollout.started_at)
