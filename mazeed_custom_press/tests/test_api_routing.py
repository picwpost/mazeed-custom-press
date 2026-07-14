"""Slice 1 — FLAG-01..07: feature flag and router behavior."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.api import release_rollout as router
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	make_website_user,
)

FLAG_FIELD = "enable_release_rollout_queue"


def set_flag(value: int):
	frappe.db.set_single_value("Press Settings", FLAG_FIELD, value)


class TestFlagAndRouter(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_flag_01_flag_defaults_to_off(self):
		# The installed field definition must default to off; the current site
		# value is an operator choice and not asserted here.
		field_default = frappe.db.get_value(
			"Custom Field", {"dt": "Press Settings", "fieldname": FLAG_FIELD}, "default"
		)
		self.assertIn(field_default, ("0", None))
		set_flag(0)
		self.assertFalse(router.rollout_queue_enabled())

	def test_flag_02_and_04_flag_off_calls_only_the_legacy_helper_and_creates_no_rollout(self):
		set_flag(0)
		with (
			patch.object(router, "run_legacy_update_all_sites") as legacy,
			patch.object(router, "create_release_rollout") as create,
		):
			router.update_all_sites(name="some-group")
		legacy.assert_called_once_with("some-group")
		create.assert_not_called()
		self.assertFalse(frappe.db.exists("Release Rollout", {"release_group": "some-group"}))

	def test_flag_03_flag_off_returns_the_legacy_response_unchanged(self):
		set_flag(0)
		sentinel = object()
		with patch.object(router, "run_legacy_update_all_sites", return_value=sentinel):
			self.assertIs(router.update_all_sites(name="some-group"), sentinel)

	def test_flag_05_and_06_flag_on_calls_only_rollout_creation_and_returns_its_response(self):
		set_flag(1)
		response = {"rollout": "ROLLOUT-2026-00001", "selected_sites": 4}
		with (
			patch.object(router, "run_legacy_update_all_sites") as legacy,
			patch.object(router, "create_release_rollout", return_value=response) as create,
		):
			result = router.update_all_sites(name="some-group")
		create.assert_called_once_with("some-group")
		legacy.assert_not_called()
		self.assertEqual(result, response)

	def test_flag_05_flag_on_creates_a_real_rollout_end_to_end(self):
		set_flag(1)
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		fabricate_site(bench)
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue"):
			result = router.update_all_sites(name=group)
		self.assertEqual(result["selected_sites"], 1)
		self.assertTrue(frappe.db.exists("Release Rollout", result["rollout"]))

	def test_flag_07_override_keeps_release_group_permission_checks(self):
		set_flag(0)
		group = fabricate_release_group(team="team-of-someone-else")
		outsider = make_website_user()
		frappe.set_user(outsider)
		with self.assertRaises(frappe.PermissionError):
			router.update_all_sites(name=group)

	def test_override_hook_points_at_the_router(self):
		overrides = frappe.get_hooks("override_whitelisted_methods")
		self.assertIn(
			"mazeed_custom_press.api.release_rollout.update_all_sites",
			overrides.get("press.api.bench.update_all_sites", []),
		)

	def test_missing_custom_field_is_safely_disabled(self):
		with patch("frappe.get_meta") as get_meta:
			get_meta.return_value.has_field.return_value = False
			self.assertFalse(router.rollout_queue_enabled())

	def test_legacy_helper_preserves_active_bench_selection(self):
		with (
			patch("frappe.get_all", return_value=[{"name": "bench-1"}]) as get_all,
			patch("frappe.get_cached_doc") as get_cached_doc,
		):
			router.run_legacy_update_all_sites("group-1")

		get_all.assert_called_once_with("Bench", {"group": "group-1", "status": "Active"})
		get_cached_doc.return_value.update_all_sites.assert_called_once_with()

	def test_flag_reads_press_settings_once(self):
		with (
			patch("frappe.get_meta") as get_meta,
			patch("frappe.db.get_single_value", return_value=1) as get_value,
		):
			get_meta.return_value.has_field.return_value = True
			self.assertTrue(router.rollout_queue_enabled())

		get_value.assert_called_once_with("Press Settings", FLAG_FIELD)
