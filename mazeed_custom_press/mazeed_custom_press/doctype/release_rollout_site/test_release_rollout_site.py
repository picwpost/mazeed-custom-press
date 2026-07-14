import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	make_rollout,
	make_rollout_site,
)


class TestReleaseRolloutSite(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_the_same_site_cannot_be_added_twice_even_at_the_database_level(self):
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		site = fabricate_site(bench)
		rollout = make_rollout(group)
		make_rollout_site(rollout.name, site, bench)
		with self.assertRaises(frappe.UniqueValidationError):
			make_rollout_site(rollout.name, site, bench)

	def test_the_same_site_may_appear_in_two_different_rollouts(self):
		group = fabricate_release_group()
		other_group = fabricate_release_group()
		bench = fabricate_bench(group)
		site = fabricate_site(bench)
		rollout = make_rollout(group, status="Completed")
		other_rollout = make_rollout(other_group)
		make_rollout_site(rollout.name, site, bench)
		row = make_rollout_site(other_rollout.name, site, bench)
		self.assertTrue(row.name)

	def test_required_database_indexes_exist(self):
		indexed_columns = {
			tuple(row)
			for row in frappe.db.sql(
				"""
				SELECT index_name, column_name FROM information_schema.statistics
				WHERE table_schema = DATABASE() AND table_name = 'tabRelease Rollout Site'
				"""
			)
		}
		index_names = {name for name, _ in indexed_columns}
		self.assertTrue(
			any("rollout" in str(columns).lower() for columns in indexed_columns),
			f"expected a (rollout, ...) index, found: {index_names}",
		)
		self.assertIn("site_update", {column for _, column in indexed_columns})
