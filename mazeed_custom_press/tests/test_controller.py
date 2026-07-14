"""Slice 3 — QUEUE-01..07: claiming and concurrency."""

import threading
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import start_next_sites
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	make_rollout,
	make_rollout_site,
)


def make_running_rollout(pending_rows=5, limit=2, **rollout_fields):
	group = fabricate_release_group()
	bench = fabricate_bench(group)
	rollout = make_rollout(group, max_concurrent_updates=limit, total_sites=pending_rows, **rollout_fields)
	rows = [
		make_rollout_site(rollout.name, fabricate_site(bench), bench)
		for _ in range(pending_rows)
	]
	return rollout, rows, bench


def status_counts(rollout_name):
	rows = frappe.get_all("Release Rollout Site", {"rollout": rollout_name}, pluck="status")
	return {status: rows.count(status) for status in set(rows)}


@patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock())
class TestQueueController(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_queue_01_an_empty_controller_starts_exactly_two_of_five_pending_rows(self):
		rollout, _, _ = make_running_rollout(pending_rows=5, limit=2)
		start_next_sites(rollout.name)
		counts = status_counts(rollout.name)
		self.assertEqual(counts.get("Starting", 0), 2)
		self.assertEqual(counts.get("Pending", 0), 3)

	def test_queue_02_running_plus_starting_never_exceeds_two(self):
		rollout, rows, _ = make_running_rollout(pending_rows=5, limit=2)
		frappe.db.set_value("Release Rollout Site", rows[0].name, "status", "Running")
		start_next_sites(rollout.name)
		counts = status_counts(rollout.name)
		self.assertEqual(counts.get("Running", 0) + counts.get("Starting", 0), 2)

	def test_queue_03_a_controller_at_capacity_starts_nothing(self):
		rollout, rows, _ = make_running_rollout(pending_rows=5, limit=2)
		frappe.db.set_value("Release Rollout Site", rows[0].name, "status", "Running")
		frappe.db.set_value("Release Rollout Site", rows[1].name, "status", "Starting")
		start_next_sites(rollout.name)
		self.assertEqual(status_counts(rollout.name).get("Pending", 0), 3)

	def test_queue_04_and_05_repeated_controller_invocations_are_harmless_and_cannot_double_claim(self):
		rollout, _, _ = make_running_rollout(pending_rows=5, limit=2)
		start_next_sites(rollout.name)
		start_next_sites(rollout.name)
		start_next_sites(rollout.name)
		counts = status_counts(rollout.name)
		self.assertEqual(counts.get("Starting", 0), 2)
		self.assertEqual(counts.get("Pending", 0), 3)

	def test_queue_04_conditional_claim_is_atomic_at_the_database_level(self):
		rollout, rows, _ = make_running_rollout(pending_rows=1, limit=2)
		row = rows[0].name
		claim = (
			"UPDATE `tabRelease Rollout Site` SET status='Starting' "
			"WHERE name=%s AND status='Pending'"
		)
		frappe.db.sql(claim, row)
		first_claim_status = frappe.db.get_value("Release Rollout Site", row, "status")
		frappe.db.sql(claim, row)
		self.assertEqual(first_claim_status, "Starting")
		self.assertEqual(frappe.db.get_value("Release Rollout Site", row, "status"), "Starting")

	def test_queue_06_priority_and_creation_order_are_respected(self):
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		rollout = make_rollout(group, max_concurrent_updates=2, total_sites=3)
		low_priority_old = make_rollout_site(rollout.name, fabricate_site(bench), bench, priority=0)
		high_priority = make_rollout_site(rollout.name, fabricate_site(bench), bench, priority=5)
		low_priority_new = make_rollout_site(rollout.name, fabricate_site(bench), bench, priority=0)

		start_next_sites(rollout.name)

		claimed = set(
			frappe.get_all(
				"Release Rollout Site",
				{"rollout": rollout.name, "status": "Starting"},
				pluck="name",
			)
		)
		self.assertEqual(claimed, {high_priority.name, low_priority_old.name})
		self.assertEqual(
			frappe.db.get_value("Release Rollout Site", low_priority_new.name, "status"), "Pending"
		)

	def test_queue_07_completed_rollouts_start_nothing(self):
		for final_status in ("Completed", "Completed With Failures", "Cancelled"):
			rollout, _, _ = make_running_rollout(pending_rows=2, limit=2, status=final_status)
			start_next_sites(rollout.name)
			self.assertEqual(status_counts(rollout.name).get("Pending", 0), 2)


class TestQueueControllerDatabaseConcurrency(FrappeTestCase):
	"""Real two-connection concurrency: the spec requires at least one
	database-level test, not only mocked calls. This test commits its fixture
	so both worker connections can see it, and cleans up after itself."""

	def test_concurrent_controllers_cannot_exceed_the_limit(self):
		site_name = frappe.local.site
		sites_path = frappe.local.sites_path
		group = fabricate_release_group()
		bench = fabricate_bench(group)
		rollout = make_rollout(group, max_concurrent_updates=2, total_sites=6)
		for _ in range(6):
			make_rollout_site(rollout.name, fabricate_site(bench), bench)
		frappe.db.commit()

		errors = []

		def controller_worker():
			try:
				frappe.init(site=site_name, sites_path=sites_path)
				frappe.connect()
				frappe.flags.in_test = True
				start_next_sites(rollout.name)
				frappe.db.commit()
			except Exception as exc:  # surfaced below; a swallowed error would fake a pass
				errors.append(exc)
				frappe.db.rollback()
			finally:
				frappe.destroy()

		try:
			with patch("mazeed_custom_press.release_rollout.frappe.enqueue", new=Mock()):
				threads = [threading.Thread(target=controller_worker) for _ in range(3)]
				for thread in threads:
					thread.start()
				for thread in threads:
					thread.join(timeout=60)

			frappe.db.commit()  # end our snapshot so the workers' commits are visible
			self.assertEqual(errors, [])
			starting = frappe.db.count(
				"Release Rollout Site", {"rollout": rollout.name, "status": "Starting"}
			)
			pending = frappe.db.count(
				"Release Rollout Site", {"rollout": rollout.name, "status": "Pending"}
			)
			self.assertEqual(starting, 2)
			self.assertEqual(pending, 4)
		finally:
			frappe.db.sql(
				"DELETE FROM `tabRelease Rollout Site` WHERE rollout=%s", rollout.name
			)
			frappe.db.sql("DELETE FROM `tabRelease Rollout` WHERE name=%s", rollout.name)
			frappe.db.sql("DELETE FROM `tabSite` WHERE bench=%s", bench)
			frappe.db.sql("DELETE FROM `tabBench` WHERE name=%s", bench)
			frappe.db.sql("DELETE FROM `tabRelease Group` WHERE name=%s", group)
			frappe.db.commit()
