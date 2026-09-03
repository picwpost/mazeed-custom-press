"""Per-server concurrency in the rollout scheduler.

Agent workers are per server while ``max_concurrent_updates`` is a total
across the rollout, so a release group spread over several servers used to
leave most of them idle. These tests pin both halves of the fix: the total is
still a hard ceiling, and no single server is allowed to consume it.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.release_rollout import start_next_sites
from mazeed_custom_press.tests.rollout_test_utils import (
	fabricate,
	fabricate_bench,
	fabricate_release_group,
	fabricate_site,
	make_rollout,
	make_rollout_site,
)


class TestPerServerConcurrency(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _bench_on_new_server(self, group: str) -> tuple[str, str]:
		server = fabricate("Server", status="Active")
		bench = fabricate_bench(group)
		frappe.db.set_value("Bench", bench, "server", server)
		return server, bench

	def _start(self, rollout_name: str) -> list[str]:
		"""Run one scheduling tick and return the rows it claimed."""
		with patch("mazeed_custom_press.release_rollout.frappe.enqueue"):
			start_next_sites(rollout_name)
		return frappe.get_all(
			"Release Rollout Site",
			filters={"rollout": rollout_name, "status": "Starting"},
			pluck="name",
		)

	def _servers_of(self, row_names: list[str]) -> list[str]:
		servers = []
		for row in row_names:
			bench = frappe.db.get_value("Release Rollout Site", row, "source_bench")
			servers.append(frappe.db.get_value("Bench", bench, "server"))
		return servers

	def test_work_is_spread_across_servers_instead_of_stacking_on_one(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=4, max_concurrent_updates_per_server=1)
		servers = []
		for _ in range(4):
			server, bench = self._bench_on_new_server(group)
			servers.append(server)
			for _ in range(3):
				make_rollout_site(rollout.name, fabricate_site(bench), bench)

		started = self._servers_of(self._start(rollout.name))

		self.assertEqual(len(started), 4, "all four global slots should be filled")
		self.assertCountEqual(started, servers, "one site per server, not four on one server")

	def test_a_single_server_is_never_given_more_than_its_own_cap(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=8, max_concurrent_updates_per_server=2)
		_, bench = self._bench_on_new_server(group)
		for _ in range(8):
			make_rollout_site(rollout.name, fabricate_site(bench), bench)

		started = self._start(rollout.name)

		self.assertEqual(len(started), 2, "one server must not consume all eight global slots")

	def test_the_total_limit_still_caps_a_rollout_spread_over_many_servers(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=3, max_concurrent_updates_per_server=2)
		for _ in range(5):
			_, bench = self._bench_on_new_server(group)
			for _ in range(2):
				make_rollout_site(rollout.name, fabricate_site(bench), bench)

		started = self._start(rollout.name)

		self.assertEqual(len(started), 3, "the rollout total is a hard ceiling, not a suggestion")

	def test_slots_already_in_use_on_a_server_are_counted_against_its_cap(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=6, max_concurrent_updates_per_server=2)
		_, busy_bench = self._bench_on_new_server(group)
		free_server, free_bench = self._bench_on_new_server(group)
		make_rollout_site(rollout.name, fabricate_site(busy_bench), busy_bench, status="Running")
		make_rollout_site(rollout.name, fabricate_site(busy_bench), busy_bench, status="Running")
		make_rollout_site(rollout.name, fabricate_site(busy_bench), busy_bench)
		make_rollout_site(rollout.name, fabricate_site(free_bench), free_bench)

		started = self._servers_of(self._start(rollout.name))

		self.assertEqual(
			started, [free_server], "the busy server is at its cap; only the idle one may start"
		)

	def test_an_unset_per_server_limit_reproduces_the_old_single_cap_behaviour(self):
		# Rollouts created before this field existed have 0 stored, and must
		# keep behaving exactly as they did: the total is the only cap.
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=3, max_concurrent_updates_per_server=0)
		_, bench = self._bench_on_new_server(group)
		for _ in range(5):
			make_rollout_site(rollout.name, fabricate_site(bench), bench)

		started = self._start(rollout.name)

		self.assertEqual(len(started), 3, "all three slots may land on one server, as before")

	def test_nothing_starts_once_the_total_limit_is_already_taken(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=2, max_concurrent_updates_per_server=2)
		_, bench_a = self._bench_on_new_server(group)
		_, bench_b = self._bench_on_new_server(group)
		make_rollout_site(rollout.name, fabricate_site(bench_a), bench_a, status="Running")
		make_rollout_site(rollout.name, fabricate_site(bench_b), bench_b, status="Running")
		make_rollout_site(rollout.name, fabricate_site(bench_b), bench_b)

		self.assertEqual(self._start(rollout.name), [])

	def test_only_canary_sites_are_picked_while_the_rollout_is_in_the_canary_stage(self):
		group = fabricate_release_group()
		rollout = make_rollout(
			group,
			max_concurrent_updates=4,
			max_concurrent_updates_per_server=2,
			stage="Canary",
			canary_status="Pending",
			canary_size=1,
		)
		_, bench = self._bench_on_new_server(group)
		canary = make_rollout_site(rollout.name, fabricate_site(bench), bench, is_canary=1)
		make_rollout_site(rollout.name, fabricate_site(bench), bench)
		make_rollout_site(rollout.name, fabricate_site(bench), bench)

		self.assertEqual(self._start(rollout.name), [canary.name])

	def test_sites_are_still_scheduled_when_their_benchs_server_is_unset(self):
		# Regression: the first version of this inner-joined Bench and matched
		# on b.server, so a bench with no server matched no bucket and its
		# sites became permanently unschedulable -- the rollout stalled at zero
		# started with no error anywhere. Nothing may become unschedulable.
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=2, max_concurrent_updates_per_server=2)
		bench = fabricate_bench(group)
		self.assertFalse(frappe.db.get_value("Bench", bench, "server"), "fixture must have no server")
		for _ in range(3):
			make_rollout_site(rollout.name, fabricate_site(bench), bench)

		self.assertEqual(len(self._start(rollout.name)), 2)

	def test_sites_are_still_scheduled_when_the_source_bench_row_is_gone(self):
		# Same failure mode via a different route: a deleted Bench leaves the
		# left join with no match at all.
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=2, max_concurrent_updates_per_server=2)
		bench = fabricate_bench(group)
		site = fabricate_site(bench)
		make_rollout_site(rollout.name, site, bench)
		frappe.db.sql("DELETE FROM `tabBench` WHERE name = %s", bench)

		self.assertEqual(len(self._start(rollout.name)), 1)

	def test_an_unset_server_is_capped_like_any_other_bucket(self):
		group = fabricate_release_group()
		rollout = make_rollout(group, max_concurrent_updates=6, max_concurrent_updates_per_server=2)
		bench = fabricate_bench(group)
		for _ in range(5):
			make_rollout_site(rollout.name, fabricate_site(bench), bench)

		self.assertEqual(len(self._start(rollout.name)), 2)
