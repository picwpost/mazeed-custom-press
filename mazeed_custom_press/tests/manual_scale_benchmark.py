"""Throwaway script: scale benchmark for Release Rollout at ~1800 sites.

Isolates THIS app's own orchestration cost (creation, locking, recount,
refill) from Press's real site-update mechanics, which were already profiled
separately against production data this session. Site.schedule_update is
mocked to insert a bare Site Update instantly -- what's being measured here
is the rollout controller's own overhead per event, not agent/network time.

Not a test module -- delete after use.
"""

import time
from unittest.mock import patch

import frappe

from mazeed_custom_press.release_rollout import (
	create_release_rollout, start_next_sites, start_rollout_site, sync_site_update,
)
from mazeed_custom_press.tests.rollout_test_utils import fabricate_bench, fabricate_release_group, fabricate_site


def drain_starting(rollout):
	"""start_next_sites only enqueues start_rollout_site as a background job (queue="short");
	with no worker consuming that queue in this script, rows would sit in "Starting" forever.
	Simulate a worker picking each one up immediately."""
	starting = frappe.get_all("Release Rollout Site", filters={"rollout": rollout, "status": "Starting"}, pluck="name")
	for row_name in starting:
		start_rollout_site(row_name)

_query_count = {"n": 0}


def _counting_sql(original):
	def wrapper(*args, **kwargs):
		_query_count["n"] += 1
		return original(*args, **kwargs)

	return wrapper


def build_group(num_sites, sites_per_bench=90):
	group = fabricate_release_group()
	sites = []
	bench = None
	for i in range(num_sites):
		if i % sites_per_bench == 0:
			bench = fabricate_bench(group)
		sites.append(fabricate_site(bench))
	return group, sites


def run(num_sites=1800, max_concurrent_updates=50):
	from press.press.doctype.site.site import Site

	group, sites = build_group(num_sites)
	frappe.db.commit()

	# --- Phase 1: creation (bulk_insert path) ---
	t0 = time.time()
	result = create_release_rollout(group, max_concurrent_updates=max_concurrent_updates, canary_size=0)
	creation_time = time.time() - t0
	rollout = result["rollout"]

	def fake_schedule_update(self, **kwargs):
		name = f"bench-site-update-{self.name}"
		frappe.get_doc({
			"doctype": "Site Update", "name": name, "site": self.name, "status": "Pending",
		}).db_insert()
		return name

	original_sql = frappe.db.sql
	frappe.db.sql = _counting_sql(original_sql)

	# --- Phase 2: ramp-up (first batch of concurrent starts) ---
	with patch.object(Site, "schedule_update", new=fake_schedule_update):
		q_before = _query_count["n"]
		t0 = time.time()
		start_next_sites(rollout)
		drain_starting(rollout)
		frappe.db.commit()
		ramp_up_time = time.time() - t0
		ramp_up_queries = _query_count["n"] - q_before

		# --- Phase 3: drive to completion one event at a time (worst case:
		# no batching of arrivals -- every completion handled individually,
		# exactly what a real fleet of concurrently-finishing sites looks like) ---
		event_times = []
		event_queries = []
		completed = 0
		t_total_start = time.time()
		while True:
			running = frappe.get_all(
				"Release Rollout Site",
				filters={"rollout": rollout, "status": "Running"},
				pluck="name", limit_page_length=1,
			)
			if not running:
				break
			row_name = running[0]
			site_update = frappe.db.get_value("Release Rollout Site", row_name, "site_update")
			frappe.db.set_value("Site Update", site_update, "status", "Success")

			q0 = _query_count["n"]
			te0 = time.time()
			sync_site_update(site_update)
			start_next_sites(rollout)  # a worker would run this from the enqueued job
			drain_starting(rollout)  # ...and this from the job start_next_sites enqueues in turn
			frappe.db.commit()  # each of the above is a separate job in production; each commits on its own
			event_times.append(time.time() - te0)
			event_queries.append(_query_count["n"] - q0)
			completed += 1
		total_cycle_time = time.time() - t_total_start

	frappe.db.sql = original_sql

	n = len(event_times)
	first_100_avg = sum(event_times[:100]) / min(100, n)
	last_100_avg = sum(event_times[-100:]) / min(100, n)
	first_100_q = sum(event_queries[:100]) / min(100, n)
	last_100_q = sum(event_queries[-100:]) / min(100, n)

	rollout_doc = frappe.get_doc("Release Rollout", rollout)

	result = {
		"num_sites": num_sites,
		"max_concurrent_updates": max_concurrent_updates,
		"creation_time_s": round(creation_time, 3),
		"ramp_up_time_s": round(ramp_up_time, 3),
		"ramp_up_queries": ramp_up_queries,
		"completed": completed,
		"total_cycle_time_s": round(total_cycle_time, 3),
		"avg_event_time_ms": round((total_cycle_time / n) * 1000, 2) if n else None,
		"first_100_avg_event_ms": round(first_100_avg * 1000, 2),
		"last_100_avg_event_ms": round(last_100_avg * 1000, 2),
		"first_100_avg_queries": round(first_100_q, 1),
		"last_100_avg_queries": round(last_100_q, 1),
		"final_status": rollout_doc.status,
		"final_success_sites": rollout_doc.success_sites,
	}
	print("=== RESULT ===")
	for k, v in result.items():
		print(f"{k}: {v}")
	return result
