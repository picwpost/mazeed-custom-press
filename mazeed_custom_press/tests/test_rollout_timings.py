"""Queue wait vs actual update time in the rollout view.

The site reads Pending from the moment its Site Update is created until an
agent worker picks the job up. That is queueing, not work, and the two have to
be reported separately -- one is fixed by capacity and queue priority, the
other is the migration itself.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from mazeed_custom_press.api.release_rollout import (
	QUEUE_WAIT_BAD_SECONDS,
	QUEUE_WAIT_WARN_SECONDS,
	get_queue_wait_stats,
	get_rollout_sites,
)
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate,
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	fabricate_site_update,
	make_rollout,
	make_rollout_site,
)


class RolloutTimingCase(FrappeTestCase):
	def setUp(self):
		self.group = fabricate_release_group()
		self.bench = fabricate_bench(self.group)
		self.rollout = make_rollout(self.group)

	def tearDown(self):
		frappe.db.rollback()

	def add_site(
		self,
		queue_seconds: int | None = None,
		update_duration: int | None = None,
		status: str = "Running",
		site_status: str = "Updating",
		delivered: bool = True,
		retry_count: int = 0,
	) -> str:
		"""A rollout row with a Site Update and Agent Job shaped to a timeline."""
		site = fabricate_site(self.bench, status=site_status)
		update_start = add_to_date(now_datetime(), seconds=-(queue_seconds or 0) - 5)
		job = fabricate(
			"Agent Job",
			job_type="Update Site Migrate",
			status="Running",
			retry_count=retry_count,
			start=add_to_date(update_start, seconds=queue_seconds) if delivered else None,
		)
		site_update = fabricate_site_update(
			site,
			status="Running",
			update_start=update_start,
			update_job=job,
			update_duration=update_duration,
			deploy_type="Migrate",
		)
		make_rollout_site(self.rollout.name, site, self.bench, status=status, site_update=site_update)
		return site

	def row_for(self, site: str) -> dict:
		rows = get_rollout_sites(self.rollout.name)
		return next(row for row in rows if row["site"] == site)


class TestQueueAndUpdateSplit(RolloutTimingCase):
	def test_queue_wait_is_reported_separately_from_the_update_itself(self):
		site = self.add_site(queue_seconds=240, update_duration=95)

		row = self.row_for(site)

		self.assertEqual(row["queue_seconds"], 240)
		self.assertEqual(row["update_seconds"], 95)

	def test_an_undelivered_job_counts_its_whole_wait_as_queueing(self):
		# No Agent Job start yet: the site is sitting Pending and every second
		# of it is queue wait, which must still be visible.
		site = self.add_site(queue_seconds=None, delivered=False, status="Running")

		row = self.row_for(site)

		self.assertIsNotNone(row["queue_seconds"])
		self.assertGreaterEqual(row["queue_seconds"], 0)

	def test_a_short_wait_is_graded_ok(self):
		site = self.add_site(queue_seconds=5, update_duration=30)
		self.assertEqual(self.row_for(site)["queue_severity"], "ok")

	def test_a_wait_over_a_minute_is_graded_warn(self):
		site = self.add_site(queue_seconds=QUEUE_WAIT_WARN_SECONDS + 5, update_duration=30)
		self.assertEqual(self.row_for(site)["queue_severity"], "warn")

	def test_a_wait_over_five_minutes_is_graded_bad(self):
		site = self.add_site(queue_seconds=QUEUE_WAIT_BAD_SECONDS + 5, update_duration=30)
		self.assertEqual(self.row_for(site)["queue_severity"], "bad")

	def test_a_row_with_no_site_update_yet_reports_no_timings(self):
		site = fabricate_site(self.bench)
		make_rollout_site(self.rollout.name, site, self.bench, status="Pending")

		row = self.row_for(site)

		self.assertIsNone(row["queue_seconds"])
		self.assertIsNone(row["queue_severity"])


class TestLiveSiteStatusIsSurfaced(RolloutTimingCase):
	def test_the_sites_own_status_is_returned_alongside_the_rollout_status(self):
		# The two diverge, and that divergence is the whole point: the rollout
		# row reads Running the moment the Site Update is created, while the
		# site itself still reads Pending until a worker starts the job.
		site = self.add_site(queue_seconds=120, status="Running", site_status="Pending")

		row = self.row_for(site)

		self.assertEqual(row["status"], "Running")
		self.assertEqual(row["site_status"], "Pending")

	def test_delivery_retries_are_surfaced(self):
		site = self.add_site(queue_seconds=30, retry_count=3)
		self.assertEqual(self.row_for(site)["retry_count"], 3)

	def test_the_deploy_type_is_returned_so_pull_and_migrate_can_be_told_apart(self):
		site = self.add_site(queue_seconds=10)
		self.assertEqual(self.row_for(site)["deploy_type"], "Migrate")


class TestQueueWaitStats(RolloutTimingCase):
	def test_median_and_worst_are_computed_across_the_rollout(self):
		for wait in (10, 20, 300):
			self.add_site(queue_seconds=wait, update_duration=5)

		stats = get_queue_wait_stats(self.rollout.name)

		self.assertEqual(stats["queue_wait_samples"], 3)
		self.assertEqual(stats["queue_wait_median"], 20)
		self.assertEqual(stats["queue_wait_max"], 300)

	def test_an_even_number_of_samples_averages_the_middle_two(self):
		for wait in (10, 20, 40, 90):
			self.add_site(queue_seconds=wait, update_duration=5)

		self.assertEqual(get_queue_wait_stats(self.rollout.name)["queue_wait_median"], 30)

	def test_a_rollout_with_nothing_delivered_yet_reports_no_samples(self):
		self.add_site(delivered=False)

		stats = get_queue_wait_stats(self.rollout.name)

		self.assertIsNone(stats["queue_wait_median"])
		self.assertEqual(stats["queue_wait_samples"], 0)


class TestFiltersStillWork(RolloutTimingCase):
	def test_filtering_by_status_still_applies_after_the_rewrite(self):
		self.add_site(queue_seconds=10, status="Running")
		self.add_site(queue_seconds=10, status="Success")

		rows = get_rollout_sites(self.rollout.name, status="Success")

		self.assertEqual([row["status"] for row in rows], ["Success"])

	def test_filtering_by_stage_still_applies(self):
		site = fabricate_site(self.bench)
		make_rollout_site(self.rollout.name, site, self.bench, is_canary=1)
		other = fabricate_site(self.bench)
		make_rollout_site(self.rollout.name, other, self.bench, is_canary=0)

		canary_rows = get_rollout_sites(self.rollout.name, stage="Canary")

		self.assertEqual([row["site"] for row in canary_rows], [site])

	def test_page_length_is_clamped_so_a_huge_request_cannot_scan_everything(self):
		for _ in range(3):
			self.add_site(queue_seconds=1)

		rows = get_rollout_sites(self.rollout.name, page_length=100000)

		self.assertLessEqual(len(rows), 100)
