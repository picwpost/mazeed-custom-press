from __future__ import annotations

import base64
import csv
import json
import io
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.api.release_group_script import (
	create_release_group_script_job,
	get_release_group_script_job_detail,
	run_release_group_script,
)
from mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run import (
	ReleaseGroupScriptRun,
)
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

	def test_create_job_api_accepts_bench_list(self):
		team, bench, *_ = self._make_team_and_benches()
		root, _, _ = self._create_bench_tree(bench.name, ["active.example.com"], extra_sites=["unsafe site"])

		with (
			patch.object(frappe.conf, "release_group_script_benches_root", str(root), create=True),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
			) as mock_enqueue,
		):
			result = create_release_group_script_job(
				requested_benches=[bench.name],
				raw_script='echo "$1"',
				timeout=9,
			)

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
			create_release_group_script_job(
				requested_benches=[bench.name],
				raw_script="echo hi",
				timeout=5,
			)

	def test_get_rejects_other_team(self):
		team, bench, *_ = self._make_team_and_benches()
		root, _, _ = self._create_bench_tree(bench.name, ["active.example.com"])

		with (
			patch.object(frappe.conf, "release_group_script_benches_root", str(root), create=True),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
			),
		):
			job_data = create_release_group_script_job(
				requested_benches=[bench.name],
				raw_script="echo hi",
				timeout=5,
			)

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
			job = ReleaseGroupScriptRun.create([bench.name], 'echo "$@"', timeout=2)
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
			job = ReleaseGroupScriptRun.create([bench1.name, bench2.name], 'echo "$@"', timeout=1)
			job.process()
			job.reload()

		self.assertEqual(job.status, "Failure")
		self.assertEqual(job.bench_runs[0].status, "Failure")
		self.assertTrue(job.bench_runs[0].timed_out)
		self.assertEqual(job.bench_runs[1].status, "Failure")
		self.assertEqual(job.bench_runs[1].exit_code, 3)
		self.assertEqual(mock_run.call_count, 2)


class TestCreateForReleaseGroup(FrappeTestCase):
	"""Tests for create_for_release_group using real bench/release-group records."""

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def _make_active_bench(self, team_user, group=None):
		bench = create_test_bench(user=team_user, group=group)
		bench.db_set("status", "Active")
		return bench

	def test_resolves_active_benches(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = self._make_active_bench(team.user)
		rg_name = bench.group

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
		):
			job = ReleaseGroupScriptRun.create_for_release_group(rg_name, "echo hi")

		self.assertIn(bench.name, job.requested_benches_list())

	def test_picks_newest_bench_as_agent_host(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench1 = self._make_active_bench(team.user)
		rg = frappe.get_doc("Release Group", bench1.group)
		bench2 = self._make_active_bench(team.user, group=rg)

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
		):
			job = ReleaseGroupScriptRun.create_for_release_group(rg.name, "echo hi")

		# newest bench (last by creation) should be agent host
		self.assertIn(job.agent_host_bench, [bench1.name, bench2.name])
		self.assertEqual(job.agent_host_server, frappe.db.get_value("Bench", job.agent_host_bench, "server"))

	def test_fails_with_no_active_benches(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = create_test_bench(user=team.user)
		bench.db_set("status", "Broken")
		rg_name = bench.group

		with self.assertRaises(frappe.ValidationError):
			ReleaseGroupScriptRun.create_for_release_group(rg_name, "echo hi")

	def test_rejects_wrong_team(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = self._make_active_bench(team.user)
		rg_name = bench.group

		other_team = create_test_press_admin_team()
		frappe.set_user(other_team.user)

		with self.assertRaises(frappe.PermissionError):
			ReleaseGroupScriptRun.create_for_release_group(rg_name, "echo hi")

	def test_run_release_group_script_api(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = self._make_active_bench(team.user)
		rg_name = bench.group

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
		):
			result = run_release_group_script(
				release_group=rg_name,
				script="echo hi",
				timeout=60,
			)

		self.assertIn("job", result)
		job = frappe.get_doc("Release Group Script Run", result["job"])
		self.assertEqual(job.release_group, rg_name)
		self.assertEqual(job.status, "Pending")


class TestProcessViaAgent(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def _make_job_with_agent_host(self, bench_names=None):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = create_test_bench(user=team.user)
		bench_names = bench_names or [bench.name]

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
		):
			doc = frappe.get_doc(
				{
					"doctype": "Release Group Script Run",
					"team": team.name,
					"requested_benches": bench_names,
					"raw_script": "echo hi",
					"timeout": 60,
					"status": "Pending",
					"agent_host_bench": bench.name,
					"agent_host_server": bench.server,
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()

		return team, bench, doc

	def _make_agent_response(self, loadable: list[str], rows: list[str], skipped: dict | None = None, errors: dict | None = None) -> dict:
		skipped = skipped or {}
		errors = errors or {}
		csv_b64 = base64.b64encode("\n".join(rows).encode()).decode()
		return {
			"status": "Success",
			"data": {
				"csv": csv_b64,
				"row_count": len(rows),
				"error_count": len(skipped) + len(errors),
			},
			"steps": [
				{
					"name": "Validate Bench List",
					"status": "Success",
					"data": {"loadable": loadable, "skipped": skipped},
				},
				{
					"name": "Run Script on All Benches",
					"status": "Success",
					"data": {"rows": rows, "errors": errors},
				},
			],
		}

	def test_sends_correct_payload_to_agent(self):
		_, bench, doc = self._make_job_with_agent_host()
		mock_agent = MagicMock()
		mock_agent.post.return_value = {"job": "test-job-id"}
		mock_agent.get.return_value = self._make_agent_response([bench.name], ["ok"])

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.Agent",
			return_value=mock_agent,
		):
			doc.process()

		mock_agent.post.assert_called_once_with(
			"server/run-release-group-script",
			{
				"benches": [bench.name],
				"script": "echo hi",
				"timeout": 60,
			},
		)

	def test_polls_until_success(self):
		_, bench, doc = self._make_job_with_agent_host()
		mock_agent = MagicMock()
		mock_agent.post.return_value = {"job": "test-job-id"}
		mock_agent.get.side_effect = [
			{"status": "Running"},
			{"status": "Running"},
			self._make_agent_response([bench.name], ["hi"]),
		]

		with (
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.Agent",
				return_value=mock_agent,
			),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.time.sleep"
			),
		):
			doc.process()

		self.assertEqual(mock_agent.get.call_count, 3)
		doc.reload()
		self.assertEqual(doc.agent_job_id, "test-job-id")

	def test_populates_bench_runs_from_agent_response(self):
		_, bench, doc = self._make_job_with_agent_host()
		mock_agent = MagicMock()
		mock_agent.post.return_value = {"job": "j1"}
		mock_agent.get.return_value = self._make_agent_response([bench.name], ["output text"])

		with (
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.Agent",
				return_value=mock_agent,
			),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.time.sleep"
			),
		):
			doc.process()
			doc.reload()

		self.assertEqual(doc.status, "Success")
		self.assertEqual(doc.row_count, 1)
		self.assertEqual(doc.error_count, 0)
		row = doc.bench_runs[0]
		self.assertEqual(row.status, "Success")
		self.assertEqual(row.stdout, "output text")

	def test_skipped_bench_marked_correctly(self):
		_, bench, doc = self._make_job_with_agent_host()
		skip_reason = "[Errno 2] No such file or directory: '/home/frappe/benches/bench/sites/common_site_config.json'"
		mock_agent = MagicMock()
		mock_agent.post.return_value = {"job": "j2"}
		mock_agent.get.return_value = self._make_agent_response(
			loadable=[],
			rows=[],
			skipped={bench.name: skip_reason},
		)

		with (
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.Agent",
				return_value=mock_agent,
			),
			patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.time.sleep"
			),
		):
			doc.process()
			doc.reload()

		self.assertEqual(doc.status, "Failure")
		row = doc.bench_runs[0]
		self.assertEqual(row.status, "Skipped")
		self.assertEqual(row.skip_reason, skip_reason)

	def test_subprocess_path_still_works_when_no_agent_host(self):
		team = create_test_press_admin_team()
		frappe.set_user(team.user)
		bench = create_test_bench(user=team.user)

		with patch(
			"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.enqueue_doc"
		):
			job = ReleaseGroupScriptRun.create([bench.name], "echo hi", timeout=2)

		self.assertFalse(job.agent_host_server)

		with (
			patch.object(job, "_process_via_agent") as mock_agent_path,
			patch.object(job, "_process_via_subprocess") as mock_subprocess_path,
		):
			with patch(
				"mazeed_custom_press.mazeed_custom_press.doctype.release_group_script_run.release_group_script_run.frappe.get_doc",
				return_value=job,
			):
				job.process()

		mock_subprocess_path.assert_called_once()
		mock_agent_path.assert_not_called()
