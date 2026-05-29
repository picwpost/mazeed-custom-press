from __future__ import annotations

import base64
import csv
import json
import io
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.api.release_group_script import (
	create_release_group_script_job,
	get_release_group_script_job_detail,
)
from mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run import (
	ReleaseGroupScriptRun,
)
from mazeed_custom_press.website.release_group_script import ReleaseGroupScriptPage
from press.press.doctype.bench.test_bench import create_test_bench
from press.press.doctype.site.test_site import create_test_site
from press.press.doctype.team.test_team import create_test_press_admin_team


class TestReleaseGroupScriptRoute(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def _make_team_and_benches(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = create_test_bench(user=team.user)
		active_site = create_test_site(bench=bench.name, team=team.name)
		standby_site = create_test_site(bench=bench.name, team=team.name)
		standby_site.db_set("is_standby", 1)
		maintenance_site = create_test_site(bench=bench.name, team=team.name)
		maintenance_site.db_set("config", json.dumps({"maintenance_mode": 1}))
		frappe.db.commit()
		return team, bench, active_site, standby_site, maintenance_site

	def _create_bench_tree(self, bench_name: str, site_names: list[str], extra_sites: list[str] | None = None):
		root = Path(tempfile.mkdtemp(prefix="release-group-script-"))
		bench_path = root / bench_name
		sites_path = bench_path / "sites"
		sites_path.mkdir(parents=True)
		(bench_path / "env").mkdir()
		for site_name in site_names + (extra_sites or []):
			(sites_path / site_name).mkdir()
		return root, bench_path, sites_path

	def test_dispatcher_accepts_exact_post_path(self):
		team, bench, *_ = self._make_team_and_benches()
		root, _, _ = self._create_bench_tree(bench.name, ["active.example.com"], extra_sites=["unsafe site"])

		with (
			patch.object(frappe.conf, "release_group_script_benches_root", str(root), create=True),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
			) as mock_enqueue,
			patch.object(frappe.local, "request", create=True) as request,
		):
			request.method = "POST"
			request.path = "/server/run-release-group-script"
			request.data = json.dumps(
				{
					"requested_benches": [bench.name],
					"raw_script": "echo \"$1\"",
					"timeout": 9,
				}
			).encode("utf-8")
			result = create_release_group_script_job()

		self.assertEqual(set(result), {"job"})
		self.assertTrue(result["job"])
		job = frappe.get_doc("Release Group Script Run", result["job"])
		self.assertEqual(job.team, team.name)
		self.assertEqual(job.status, "Pending")
		mock_enqueue.assert_called_once()

	def test_post_rejects_other_team(self):
		team, bench, *_ = self._make_team_and_benches()
		other_team = create_test_press_admin_team()
		frappe.set_user(other_team.user)

		with self.assertRaises(frappe.PermissionError):
			with patch.object(frappe.local, "request", create=True) as request:
				request.method = "POST"
				request.path = "/server/run-release-group-script"
				request.data = json.dumps(
					{
						"requested_benches": [bench.name],
						"raw_script": "echo hi",
						"timeout": 5,
					}
				).encode("utf-8")
				create_release_group_script_job()

	def test_get_rejects_other_team(self):
		team, bench, *_ = self._make_team_and_benches()
		root, _, _ = self._create_bench_tree(bench.name, ["active.example.com"])

		with (
			patch.object(frappe.conf, "release_group_script_benches_root", str(root), create=True),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
			),
			patch.object(frappe.local, "request", create=True) as request,
		):
			request.method = "POST"
			request.path = "/server/run-release-group-script"
			request.data = json.dumps(
				{
					"requested_benches": [bench.name],
					"raw_script": "echo hi",
					"timeout": 5,
				}
			).encode("utf-8")
			job_data = create_release_group_script_job()

		other_team = create_test_press_admin_team()
		frappe.set_user(other_team.user)
		with self.assertRaises(frappe.PermissionError):
			get_release_group_script_job_detail(job_data["job"])

	def test_bench_filtering_and_result_encoding(self):
		team, bench, active_site, standby_site, maintenance_site = self._make_team_and_benches()
		root, _, _ = self._create_bench_tree(
			bench.name,
			[active_site.name, standby_site.name, maintenance_site.name, "unsafe site"],
		)

		with (
			patch.object(frappe.conf, "release_group_script_benches_root", str(root), create=True),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
			),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.subprocess.run"
			) as mock_run,
		):
			mock_run.return_value = Mock(returncode=0, stdout="hello\n", stderr="")
			job = ReleaseGroupScriptRun.create([bench.name], "echo \"$@\"", timeout=2)
			job.process()
			job.reload()

		row = job.bench_runs[0]
		self.assertEqual(row.status, "Success")
		self.assertEqual(json.loads(row.sites), [active_site.name])
		self.assertEqual(row.stdout, "hello\n")
		self.assertEqual(job.status, "Success")
		decoded_payload = base64.b64decode(job.result_payload).decode("utf-8")
		self.assertIn(active_site.name, decoded_payload)
		decoded_rows = list(csv.DictReader(io.StringIO(decoded_payload)))
		self.assertEqual(decoded_rows[0]["stdout"], "hello\n")
		detail = get_release_group_script_job_detail(job.name)
		self.assertEqual(detail["status"], "Success")
		self.assertEqual(detail["result_payload"], job.result_payload)
		self.assertEqual(detail["benches"][0]["sites"], [active_site.name])
		self.assertEqual(mock_run.call_args.args[0][2:], [active_site.name])

	def test_unloadable_bench_is_skipped_and_counts_as_failure(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = create_test_bench(user=team.user)

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
		):
			job = ReleaseGroupScriptRun.create([bench.name], "echo hi", timeout=2)
			job.process()
			job.reload()

		row = job.bench_runs[0]
		self.assertEqual(row.status, "Skipped")
		self.assertEqual(row.skip_reason, "unloadable bench")
		self.assertEqual(job.status, "Failure")

	def test_timeout_and_non_zero_exit_are_recorded_without_stopping(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench1 = create_test_bench(user=team.user)
		bench2 = create_test_bench(user=team.user)
		site1 = create_test_site(bench=bench1.name, team=team.name)
		site2 = create_test_site(bench=bench2.name, team=team.name)

		root = Path(tempfile.mkdtemp(prefix="release-group-script-root-"))
		(root / bench1.name / "sites").mkdir(parents=True)
		(root / bench1.name / "env").mkdir()
		(root / bench1.name / "sites" / site1.name).mkdir()
		(root / bench2.name / "sites").mkdir(parents=True)
		(root / bench2.name / "env").mkdir()
		(root / bench2.name / "sites" / site2.name).mkdir()

		def fake_run(cmd, cwd, capture_output, text, check, timeout):
			if cwd.endswith(bench1.name):
				raise subprocess.TimeoutExpired(cmd, timeout, output="partial", stderr="timed out")
			return Mock(returncode=3, stdout="done", stderr="boom")

		with (
			patch.object(frappe.conf, "release_group_script_benches_root", str(root), create=True),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
			),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.subprocess.run",
				side_effect=fake_run,
			) as mock_run,
		):
			job = ReleaseGroupScriptRun.create([bench1.name, bench2.name], "echo \"$@\"", timeout=1)
			job.process()
			job.reload()

		self.assertEqual(job.status, "Failure")
		self.assertEqual(job.bench_runs[0].status, "Failure")
		self.assertTrue(job.bench_runs[0].timed_out)
		self.assertEqual(job.bench_runs[1].status, "Failure")
		self.assertEqual(job.bench_runs[1].exit_code, 3)
		self.assertEqual(mock_run.call_count, 2)

	def test_route_shape_does_not_accept_api_method_path(self):
		with patch.object(frappe.local, "request", create=True) as request:
			request.method = "POST"
			request.path = "/server/run-release-group-script"
			renderer = ReleaseGroupScriptPage("server/run-release-group-script")
			self.assertTrue(renderer.can_render())

			api_renderer = ReleaseGroupScriptPage("api/method/server/run-release-group-script")
			request.path = "/api/method/server/run-release-group-script"
			self.assertFalse(api_renderer.can_render())

			request.method = "GET"
			request.path = "/jobs/123"
			jobs_renderer = ReleaseGroupScriptPage("jobs/123")
			self.assertTrue(jobs_renderer.can_render())

			invalid_renderer = ReleaseGroupScriptPage("jobs/not-a-number")
			request.path = "/jobs/not-a-number"
			self.assertFalse(invalid_renderer.can_render())
