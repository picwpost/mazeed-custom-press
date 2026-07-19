"""One-off seeder for a demo Release Rollout (manual UI review). Safe to delete."""


def seed_scale(count=1000):
	"""A group with N fabricated sites, to measure snapshot cost at scale."""
	import frappe

	from mazeed_custom_press.tests.rollout_test_utils import fabricate

	if frappe.db.exists("Release Group", "demo-scale-group"):
		print("demo-scale-group already exists")
		return
	group = fabricate("Release Group", name="demo-scale-group", title="Demo Scale Group")
	bench_doc = fabricate("Bench", name="demo-scale-bench-01", group=group, status="Active")
	for index in range(1, int(count) + 1):
		fabricate("Site", name=f"demo-scale-{index:04d}.mazeed.cloud", bench=bench_doc, status="Active")
	frappe.db.commit()
	print(f"seeded demo-scale-group with {count} sites")


def seed_live():
	"""A second group with plain sites, for firing a real Update All Sites."""
	import frappe

	from mazeed_custom_press.tests.rollout_test_utils import fabricate

	if frappe.db.exists("Release Group", "demo-live-group"):
		print("demo-live-group already exists")
		return
	group = fabricate("Release Group", name="demo-live-group", title="Demo Live Group")
	bench_doc = fabricate("Bench", name="demo-live-bench-01", group=group, status="Active")
	for index in range(1, 7):
		fabricate("Site", name=f"demo-live-{index:02d}.mazeed.cloud", bench=bench_doc, status="Active")
	frappe.db.commit()
	print("seeded demo-live-group with 6 sites")

import frappe
from frappe.utils import add_to_date, now_datetime

from mazeed_custom_press.release_rollout import _recount
from mazeed_custom_press.tests.rollout_test_utils import fabricate, make_rollout, make_rollout_site


def seed():
	if frappe.db.exists("Release Group", "demo-release-group"):
		print("demo data already exists")
		rollout = frappe.db.get_value("Release Rollout", {"release_group": "demo-release-group"})
		print(f"rollout: {rollout}")
		return

	group = fabricate("Release Group", name="demo-release-group", title="Demo Release Group")
	bench_one = fabricate("Bench", name="demo-bench-01", group=group, status="Active")
	bench_two = fabricate("Bench", name="demo-bench-02", group=group, status="Active")

	now = now_datetime()
	rollout = make_rollout(
		group,
		max_concurrent_updates=3,
		canary_size=2,
		stage="Main",
		canary_status="Passed",
		total_sites=12,
		canary_started_at=add_to_date(now, minutes=-42),
		canary_finished_at=add_to_date(now, minutes=-31),
		started_at=add_to_date(now, minutes=-42),
	)

	rows = [
		# site, bench, status, is_canary, started min ago, finished min ago, error
		("demo-site-01", bench_one, "Success", 1, 42, 35, None),
		("demo-site-02", bench_one, "Recovered", 1, 42, 31, None),
		("demo-site-03", bench_one, "Success", 0, 30, 24, None),
		("demo-site-04", bench_one, "Fatal", 0, 29, 18, "Update Site Migrate failed: patch error in custom_app"),
		("demo-site-05", bench_two, "Skipped", 0, 25, 25, "Site is no longer eligible or has moved to another bench"),
		("demo-site-06", bench_two, "Cancelled", 0, 24, 15, None),
		("demo-site-07", bench_one, "Running", 0, 12, None, None),
		("demo-site-08", bench_two, "Running", 0, 8, None, None),
		("demo-site-09", bench_one, "Starting", 0, 1, None, None),
		("demo-site-10", bench_one, "Pending", 0, None, None, None),
		("demo-site-11", bench_two, "Pending", 0, None, None, None),
		("demo-site-12", bench_two, "Pending", 0, None, None, None),
	]
	for site_name, bench, status, is_canary, started, finished, error in rows:
		site = fabricate(
			"Site", name=f"{site_name}.mazeed.cloud", bench=bench, status="Active"
		)
		row = make_rollout_site(rollout.name, site, bench, status=status, is_canary=is_canary)
		values = {}
		if started is not None:
			values["started_at"] = add_to_date(now, minutes=-started)
		if finished is not None:
			values["finished_at"] = add_to_date(now, minutes=-finished)
		if error:
			values["last_error"] = error
		if status in ("Success", "Recovered", "Fatal", "Running"):
			update = fabricate("Site Update", site=site, status=status if status != "Running" else "Running")
			values["site_update"] = update
		if values:
			frappe.db.set_value("Release Rollout Site", row.name, values, update_modified=False)

	_recount(rollout.name)
	frappe.db.set_value("Release Rollout", rollout.name, "last_reconciled_at", now)
	frappe.db.commit()
	print(f"rollout: {rollout.name}")
