"""Choosing which sites gate a rollout.

Selection has three sources in descending precedence: an explicit list for one
rollout, sites standing-flagged on Site, and the original first-N-by-name.
These tests pin the precedence, the strict validation on an explicit list, and
the deliberately lenient handling of a stale flag.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import (
	CANARY_FLAG_FIELD,
	create_release_rollout,
)
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
)


class CanarySelectionCase(FrappeTestCase):
	def setUp(self):
		self.group = fabricate_release_group()
		self.bench = fabricate_bench(self.group)

	def tearDown(self):
		frappe.db.rollback()

	def make_sites(self, count: int) -> list[str]:
		"""Sites named so alphabetical order is predictable and not the choice."""
		return [
			fabricate_site(self.bench, name=f"zz-site-{index:02d}.test") for index in range(count)
		]

	def flag(self, site: str):
		frappe.db.set_value("Site", site, CANARY_FLAG_FIELD, 1)

	def start(self, **kwargs) -> str:
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue"):
			result = create_release_rollout(self.group, **kwargs)
		return result["rollout"]

	def canaries_of(self, rollout: str) -> list[str]:
		return sorted(
			frappe.get_all(
				"Release Rollout Site", {"rollout": rollout, "is_canary": 1}, pluck="site"
			)
		)


class TestExplicitCanarySites(CanarySelectionCase):
	def test_only_the_named_sites_become_canaries(self):
		sites = self.make_sites(5)
		chosen = [sites[3], sites[4]]

		rollout = self.start(canary_sites=chosen)

		self.assertEqual(self.canaries_of(rollout), sorted(chosen))

	def test_the_named_sites_win_over_alphabetical_order(self):
		# The whole point: the last site by name must be selectable, which the
		# old first-N-by-name behaviour could never produce.
		sites = self.make_sites(6)
		rollout = self.start(canary_sites=[sites[-1]])

		self.assertEqual(self.canaries_of(rollout), [sites[-1]])
		self.assertNotIn(sites[0], self.canaries_of(rollout))

	def test_canary_size_is_taken_from_the_list_not_the_setting(self):
		sites = self.make_sites(5)
		rollout = self.start(canary_sites=[sites[0], sites[1], sites[2]])

		self.assertEqual(frappe.db.get_value("Release Rollout", rollout, "canary_size"), 3)

	def test_a_list_arriving_as_a_json_string_is_accepted(self):
		# This is how the argument reaches a whitelisted method over HTTP.
		sites = self.make_sites(3)
		rollout = self.start(canary_sites=frappe.as_json([sites[1]]))

		self.assertEqual(self.canaries_of(rollout), [sites[1]])

	def test_duplicates_in_the_list_do_not_inflate_the_canary_count(self):
		sites = self.make_sites(3)
		rollout = self.start(canary_sites=[sites[0], sites[0], sites[1]])

		self.assertEqual(frappe.db.get_value("Release Rollout", rollout, "canary_size"), 2)

	def test_naming_every_site_is_allowed(self):
		sites = self.make_sites(3)
		rollout = self.start(canary_sites=sites)

		self.assertEqual(self.canaries_of(rollout), sorted(sites))
		self.assertEqual(frappe.db.get_value("Release Rollout", rollout, "stage"), "Canary")


class TestExplicitCanaryValidation(CanarySelectionCase):
	def test_a_suspended_site_is_refused_with_its_status_named(self):
		sites = self.make_sites(3)
		frappe.db.set_value("Site", sites[1], "status", "Broken")

		with self.assertRaises(frappe.ValidationError) as error:
			self.start(canary_sites=[sites[1]])

		message = str(error.exception)
		self.assertIn(sites[1], message)
		self.assertIn("Broken", message)

	def test_a_site_on_another_bench_is_refused_with_the_bench_named(self):
		self.make_sites(2)
		other_bench = fabricate_bench(fabricate_release_group())
		outsider = fabricate_site(other_bench, name="zz-outsider.test")

		with self.assertRaises(frappe.ValidationError) as error:
			self.start(canary_sites=[outsider])

		message = str(error.exception)
		self.assertIn(outsider, message)
		self.assertIn(other_bench, message)

	def test_a_site_that_does_not_exist_is_refused(self):
		self.make_sites(2)

		with self.assertRaises(frappe.ValidationError) as error:
			self.start(canary_sites=["zz-nope.test"])

		self.assertIn("no such site", str(error.exception))

	def test_every_bad_site_is_reported_in_one_error_not_just_the_first(self):
		sites = self.make_sites(3)
		frappe.db.set_value("Site", sites[0], "status", "Broken")

		with self.assertRaises(frappe.ValidationError) as error:
			self.start(canary_sites=[sites[0], "zz-nope.test"])

		message = str(error.exception)
		self.assertIn(sites[0], message)
		self.assertIn("zz-nope.test", message)

	def test_nothing_is_created_when_validation_fails(self):
		self.make_sites(2)
		before = frappe.db.count("Release Rollout", {"release_group": self.group})

		with self.assertRaises(frappe.ValidationError):
			self.start(canary_sites=["zz-nope.test"])

		self.assertEqual(frappe.db.count("Release Rollout", {"release_group": self.group}), before)

	def test_an_empty_list_is_refused_rather_than_silently_skipping_the_gate(self):
		self.make_sites(2)

		with self.assertRaises(frappe.ValidationError):
			self.start(canary_sites=[])

	def test_passing_both_a_list_and_a_size_is_refused_as_ambiguous(self):
		sites = self.make_sites(3)

		with self.assertRaises(frappe.ValidationError) as error:
			self.start(canary_sites=[sites[0]], canary_size=2)

		self.assertIn("not both", str(error.exception))


class TestFlaggedCanarySites(CanarySelectionCase):
	def test_a_flagged_site_is_used_without_being_named(self):
		sites = self.make_sites(5)
		self.flag(sites[4])

		rollout = self.start()

		self.assertEqual(self.canaries_of(rollout), [sites[4]])

	def test_every_flagged_site_in_the_group_is_used(self):
		sites = self.make_sites(5)
		self.flag(sites[1])
		self.flag(sites[3])

		rollout = self.start()

		self.assertEqual(self.canaries_of(rollout), sorted([sites[1], sites[3]]))

	def test_an_explicit_list_overrides_the_flag_for_one_rollout(self):
		sites = self.make_sites(4)
		self.flag(sites[0])

		rollout = self.start(canary_sites=[sites[2]])

		self.assertEqual(self.canaries_of(rollout), [sites[2]])

	def test_a_flagged_site_outside_this_group_is_ignored_not_an_error(self):
		# The flag is global while a rollout covers one group, so this is an
		# intersection rather than a mistake.
		sites = self.make_sites(3)
		stranger = fabricate_site(fabricate_bench(fabricate_release_group()), name="zz-stranger.test")
		self.flag(stranger)
		self.flag(sites[2])

		rollout = self.start()

		self.assertEqual(self.canaries_of(rollout), [sites[2]])

	def test_a_stale_flag_on_an_archived_site_never_blocks_the_rollout(self):
		# Deliberately lenient, unlike an explicit list: a standing preference
		# left on a site that was later archived must not break every deploy.
		sites = self.make_sites(3)
		self.flag(sites[0])
		frappe.db.set_value("Site", sites[0], "status", "Archived")

		rollout = self.start()

		self.assertNotIn(sites[0], self.canaries_of(rollout))
		self.assertEqual(frappe.db.get_value("Release Rollout", rollout, "status"), "Running")


class TestFallbackSelection(CanarySelectionCase):
	def test_with_no_list_and_no_flag_the_first_sites_by_name_are_used(self):
		sites = self.make_sites(5)

		rollout = self.start(canary_size=2)

		self.assertEqual(self.canaries_of(rollout), sorted(sites[:2]))

	def test_canary_size_zero_still_skips_the_gate_entirely(self):
		self.make_sites(3)

		rollout = self.start(canary_size=0)

		self.assertEqual(self.canaries_of(rollout), [])
		self.assertEqual(frappe.db.get_value("Release Rollout", rollout, "stage"), "Main")
		self.assertEqual(frappe.db.get_value("Release Rollout", rollout, "canary_status"), "Passed")
