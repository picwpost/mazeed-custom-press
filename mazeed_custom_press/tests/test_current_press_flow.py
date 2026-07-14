"""Slice 0 — OLD-01..07: characterization of the current Press flow.

These tests pin the Press behavior the flag-off path depends on. If Press
changes underneath us, these fail first. They do not test our app.
"""

from unittest.mock import Mock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.tests.rollout_test_utils import (
	create_updateable_site_environment,
	fabricate,
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
)


class TestCurrentPressEndpoint(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_old_01_release_endpoint_selects_only_active_benches(self):
		from press.api.bench import update_all_sites as press_update_all_sites
		from press.press.doctype.bench.bench import Bench

		group = fabricate_release_group()
		active_bench = fabricate_bench(group, status="Active")
		fabricate_bench(group, status="Archived")
		fabricate_bench(group, status="Broken")

		with patch.object(Bench, "update_all_sites") as bench_update:
			press_update_all_sites(name=group)

		self.assertEqual(bench_update.call_count, 1)
		frappe.clear_document_cache("Bench", active_bench)

	def test_old_02_and_03_bench_updates_active_inactive_suspended_sites_once_each(self):
		from press.press.doctype.site.site import Site

		group = fabricate_release_group()
		bench = fabricate_bench(group)
		fabricate_site(bench, status="Active")
		fabricate_site(bench, status="Inactive")
		fabricate_site(bench, status="Suspended")
		fabricate_site(bench, status="Archived")
		fabricate_site(bench, status="Broken")

		with (
			patch.object(Site, "schedule_update") as schedule_update,
			patch.object(frappe.db, "commit") as commit,
			patch.object(frappe.db, "rollback") as rollback,
		):
			frappe.get_doc("Bench", bench).update_all_sites()

		self.assertEqual(schedule_update.call_count, 3)
		# Characterizes the current fan-out: one commit per site, no rollbacks.
		self.assertEqual(commit.call_count, 3)
		rollback.assert_not_called()

	def test_old_04_a_site_exception_does_not_prevent_later_sites(self):
		from press.press.doctype.site.site import Site

		group = fabricate_release_group()
		bench = fabricate_bench(group)
		for _ in range(3):
			fabricate_site(bench, status="Active")

		schedule_update = Mock(side_effect=[Exception("first site broke"), None, None])
		with (
			patch.object(Site, "schedule_update", schedule_update),
			patch.object(frappe.db, "commit") as commit,
			patch.object(frappe.db, "rollback") as rollback,
		):
			frappe.get_doc("Bench", bench).update_all_sites()

		self.assertEqual(schedule_update.call_count, 3)
		self.assertEqual(commit.call_count, 2)
		self.assertEqual(rollback.call_count, 1)


class TestCurrentSiteUpdateLifecycle(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_old_05_site_update_without_scheduled_time_starts_after_insert(self):
		from press.press.doctype.agent_job.agent_job import AgentJob
		from press.press.doctype.site_update.site_update import SiteUpdate

		environment = create_updateable_site_environment()
		with (
			patch.object(AgentJob, "enqueue_http_request", new=Mock()),
			patch.object(SiteUpdate, "start") as start,
		):
			environment.site.schedule_update()
		start.assert_called_once()

	def test_old_06_site_update_failure_may_proceed_to_recovery(self):
		from press.press.doctype.agent_job.agent_job import AgentJob
		from press.press.doctype.site_update.site_update import (
			SiteUpdate,
			process_update_site_job_update,
		)

		environment = create_updateable_site_environment()
		with patch.object(AgentJob, "enqueue_http_request", new=Mock()):
			site_update_name = environment.site.schedule_update()

		update_job = frappe.db.get_value("Site Update", site_update_name, "update_job")
		frappe.db.set_value("Agent Job", update_job, "status", "Failure", update_modified=False)
		job = frappe.get_doc("Agent Job", update_job)

		with patch.object(SiteUpdate, "trigger_recovery_job") as trigger_recovery:
			process_update_site_job_update(job)

		self.assertEqual(frappe.db.get_value("Site Update", site_update_name, "status"), "Failure")
		trigger_recovery.assert_called_once()

	def test_old_07_success_recovered_and_fatal_are_terminal_bookkept_outcomes(self):
		from press.press.doctype.agent_job.agent_job import AgentJob
		from press.press.doctype.site_update.site_update import update_status

		environment = create_updateable_site_environment()
		for terminal_status in ("Success", "Recovered", "Fatal"):
			with patch.object(AgentJob, "enqueue_http_request", new=Mock()):
				site_update_name = environment.site.schedule_update()
			update_status(site_update_name, terminal_status)
			self.assertEqual(
				frappe.db.get_value("Site Update", site_update_name, "status"), terminal_status
			)
			# Press stamps update_end on Success/Failure/Fatal/Recovered
			self.assertTrue(frappe.db.get_value("Site Update", site_update_name, "update_end"))
			# Purge the record: Press refuses to schedule again for a candidate
			# pair that has a Fatal update in its history.
			frappe.db.sql("DELETE FROM `tabSite Update` WHERE name=%s", site_update_name)
