import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.tests.rollout_test_utils import fabricate_release_group, make_rollout


class TestReleaseRollout(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_max_concurrent_updates_must_be_greater_than_zero(self):
		group = fabricate_release_group()
		for invalid_limit in (0, -1):
			with self.assertRaises(frappe.ValidationError) as context:
				make_rollout(group, max_concurrent_updates=invalid_limit)
			self.assertIn("greater than zero", str(context.exception))

	def test_canary_size_cannot_be_negative(self):
		group = fabricate_release_group()
		with self.assertRaises(frappe.ValidationError) as context:
			make_rollout(group, canary_size=-1)
		self.assertIn("negative", str(context.exception))

	def test_canary_size_cannot_exceed_total_sites(self):
		group = fabricate_release_group()
		with self.assertRaises(frappe.ValidationError) as context:
			make_rollout(group, canary_size=5, total_sites=3)
		self.assertIn("exceed", str(context.exception))

	def test_a_valid_rollout_inserts(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=2, canary_size=2, total_sites=4)
		self.assertTrue(rollout.name.startswith("ROLLOUT-"))
