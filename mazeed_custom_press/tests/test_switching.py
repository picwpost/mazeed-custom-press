"""Slice 9 — SWITCH-01..04: production switching and kill-switch behavior."""

import inspect
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

import mazeed_custom_press.release_rollout as controller_module
from mazeed_custom_press.api import release_rollout as router
from mazeed_custom_press.release_rollout import (
	reconcile_running_rollouts,
	start_next_sites,
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

FLAG_FIELD = "enable_release_rollout_queue"


class TestProductionSwitching(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_switch_01_a_rollout_created_with_the_flag_on_continues_after_flag_off(self):
		frappe.db.set_single_value("Press Settings", FLAG_FIELD, 1)
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		rollout = make_rollout(group, total_sites=3)
		rows = [make_rollout_site(rollout.name, fabricate_site(bench), bench) for _ in range(3)]

		frappe.db.set_single_value("Press Settings", FLAG_FIELD, 0)  # kill switch

		with patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock()):
			start_next_sites(rollout.name)
			claimed = frappe.db.count(
				"Release Rollout Site", {"rollout": rollout.name, "status": "Starting"}
			)
			self.assertEqual(claimed, 2)

			# and completion observation still works
			update = fabricate_site_update(rows[0].site, status="Success", release_rollout_site=rows[0].name)
			frappe.db.set_value(
				"Release Rollout Site", rows[0].name, {"status": "Running", "site_update": update}
			)
			sync_site_update(update)
			self.assertEqual(
				frappe.db.get_value("Release Rollout Site", rows[0].name, "status"), "Success"
			)

			reconcile_running_rollouts()
			self.assertEqual(frappe.db.get_value("Release Rollout", rollout.name, "status"), "Running")

	def test_switch_02_a_new_request_after_flag_off_uses_the_legacy_flow(self):
		frappe.db.set_single_value("Press Settings", FLAG_FIELD, 0)
		with (
			patch.object(router, "run_legacy_update_all_sites") as legacy,
			patch.object(router, "create_release_rollout") as create,
		):
			router.update_all_sites(name="some-group")
		legacy.assert_called_once()
		create.assert_not_called()

	def test_switch_03_controller_and_reconciliation_never_consult_the_flag(self):
		source = inspect.getsource(controller_module)
		self.assertNotIn(FLAG_FIELD, source)

		# Belt and braces: even a poisoned flag reader must not affect the controller.
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		rollout = make_rollout(group, total_sites=1)
		make_rollout_site(rollout.name, fabricate_site(bench), bench)
		with (
			patch.object(router, "rollout_queue_enabled", side_effect=AssertionError("flag was read")),
			patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock()),
		):
			start_next_sites(rollout.name)
			reconcile_running_rollouts()

	def test_switch_04_the_old_api_response_remains_supported_while_off(self):
		frappe.db.set_single_value("Press Settings", FLAG_FIELD, 0)
		group = fabricate_release_group()
		fabricate_bench(group, status="Archived")  # no active benches: legacy is a silent no-op
		result = router.update_all_sites(name=group)
		self.assertIsNone(result)  # the legacy endpoint returns nothing
		self.assertFalse(frappe.db.exists("Release Rollout", {"release_group": group}))
