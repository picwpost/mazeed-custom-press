from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

import frappe
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from press.utils import get_current_team, log_error


SAFE_SITE_NAME_RE = re.compile(
		r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)
DEFAULT_BENCHES_ROOT = "/home/frappe/benches"
MIN_TIMEOUT = 1
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 300


class ReleaseGroupScriptRun(Document):
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bench_runs: DF.Table["ReleaseGroupScriptRunBench"]
		end: DF.Datetime | None
		raw_script: DF.Code
		requested_benches: DF.Code
		result_payload: DF.Code | None
		start: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Success", "Failure"]
		team: DF.Link
		timeout: DF.Int
		duration: DF.Duration | None

	def validate(self):
		self.timeout = self._clamp_timeout(self.timeout)
		self.requested_benches = json.dumps(self.requested_benches_list(), separators=(",", ":"))
		self.raw_script = self.raw_script or ""

	def before_insert(self):
		if not self.team:
			self.team = get_current_team()
		self.status = self.status or "Pending"
		self._sync_bench_rows()

	def after_insert(self):
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"process",
			queue="long",
			timeout=self._enqueue_timeout(),
			enqueue_after_commit=True,
		)

	def on_change(self):
		self.publish_update()

	def requested_benches_list(self) -> list[str]:
		value = self.requested_benches
		if not value:
			return []
		if isinstance(value, str):
			try:
				value = json.loads(value)
			except Exception:
				return [item.strip() for item in value.split(",") if item.strip()]
		return [str(bench).strip() for bench in value if str(bench).strip()]

	def _sync_bench_rows(self):
		self.set("bench_runs", [])
		for bench_name in self.requested_benches_list():
			self.append(
				"bench_runs",
				{
					"bench": bench_name,
					"status": "Pending",
				},
			)

	def _enqueue_timeout(self) -> int:
		timeout = self._clamp_timeout(self.timeout)
		bench_count = max(1, len(self.requested_benches_list()))
		return max(600, timeout * bench_count + 60)

	def process(self):
		self = frappe.get_doc(self.doctype, self.name)
		self.status = "Running"
		self.start = now_datetime()
		self.save(ignore_permissions=True)
		frappe.db.commit()

		has_failures = False

		for index, bench_name in enumerate(self.requested_benches_list()):
			row = self.bench_runs[index]
			row.status = "Running"
			self.save(ignore_permissions=True)
			frappe.db.commit()

			try:
				bench_result = self._process_bench(bench_name)
				self._apply_bench_result(row, bench_result)
				if bench_result["status"] != "Success":
					has_failures = True
			except Exception as exc:
				has_failures = True
				traceback = frappe.get_traceback(with_context=True)
				row.status = "Failure"
				row.error = traceback
				row.skip_reason = ""
				row.stdout = ""
				row.stderr = ""
				row.exit_code = None
				row.timed_out = 0
				row.sites = "[]"
				log_error(
					"Release Group Script Run Bench Failure",
					doc=self,
					bench=bench_name,
					error=str(exc),
					reference_doctype=self.doctype,
					reference_name=self.name,
				)

			self.save(ignore_permissions=True)
			frappe.db.commit()

		self.end = now_datetime()
		self.duration = self.end - self.start
		self.status = "Failure" if has_failures else "Success"
		self.result_payload = self._build_result_payload()
		self.save(ignore_permissions=True)
		frappe.db.commit()
		self.publish_update()

	def _apply_bench_result(self, row, bench_result: dict):
		row.status = bench_result["status"]
		row.skip_reason = bench_result.get("skip_reason") or ""
		row.stdout = bench_result.get("stdout") or ""
		row.stderr = bench_result.get("stderr") or ""
		row.exit_code = bench_result.get("exit_code")
		row.timed_out = cint(bench_result.get("timed_out") or 0)
		row.sites = json.dumps(bench_result.get("sites") or [], separators=(",", ":"))
		row.error = bench_result.get("error") or ""

	def _process_bench(self, bench_name: str) -> dict:
		bench_path = self._bench_path(bench_name)
		if not self._is_loadable_bench(bench_path):
			return {
				"bench": bench_name,
				"status": "Skipped",
				"skip_reason": "unloadable bench",
				"sites": [],
				"stdout": "",
				"stderr": "",
				"exit_code": None,
				"timed_out": 0,
				"error": "Bench directory is missing or unreadable.",
			}

		sites = self._load_active_sites(bench_name, bench_path)
		if not sites:
			return {
				"bench": bench_name,
				"status": "Skipped",
				"skip_reason": "no eligible sites",
				"sites": [],
				"stdout": "",
				"stderr": "",
				"exit_code": None,
				"timed_out": 0,
				"error": "",
			}

		return self._run_script_on_bench(bench_name, bench_path, sites)

	def _run_script_on_bench(self, bench_name: str, bench_path: Path, sites: list[str]) -> dict:
		with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8") as tmp:
			tmp.write(self.raw_script)
			tmp_path = Path(tmp.name)

		try:
			os.chmod(tmp_path, 0o700)
			try:
				completed = subprocess.run(
					["bash", str(tmp_path), *sites],
					cwd=str(bench_path),
					capture_output=True,
					text=True,
					check=False,
					timeout=self.timeout,
				)
				return {
					"bench": bench_name,
					"status": "Success" if completed.returncode == 0 else "Failure",
					"skip_reason": "",
					"sites": sites,
					"stdout": completed.stdout or "",
					"stderr": completed.stderr or "",
					"exit_code": completed.returncode,
					"timed_out": 0,
					"error": "",
				}
			except subprocess.TimeoutExpired as exc:
				return {
					"bench": bench_name,
					"status": "Failure",
					"skip_reason": "",
					"sites": sites,
					"stdout": self._coerce_text(exc.stdout),
					"stderr": self._coerce_text(exc.stderr),
					"exit_code": None,
					"timed_out": 1,
					"error": f"Timed out after {self.timeout} seconds.",
				}
		finally:
			with suppress(FileNotFoundError):
				tmp_path.unlink()

	def _load_active_sites(self, _bench_name: str, bench_path: Path) -> list[str]:
		sites_path = bench_path / "sites"
		if not sites_path.is_dir():
			return []

		active_sites: list[str] = []
		for site_dir in sorted(sites_path.iterdir()):
			if not site_dir.is_dir():
				continue

			site_name = site_dir.name
			if not self._is_safe_site_name(site_name):
				continue

			if not frappe.db.exists("Site", site_name):
				continue

			site = frappe.get_doc("Site", site_name)
			if site.status == "Archived" or site.is_standby:
				continue

			if self._is_site_in_maintenance(site):
				continue

			active_sites.append(site_name)

		return active_sites

	def _is_site_in_maintenance(self, site) -> bool:
		config = site.get("config") or "{}"
		if isinstance(config, str):
			try:
				config = json.loads(config)
			except Exception:
				return False
		return bool((config or {}).get("maintenance_mode"))

	def _is_safe_site_name(self, site_name: str) -> bool:
		return bool(SAFE_SITE_NAME_RE.fullmatch(site_name))

	def _bench_path(self, bench_name: str) -> Path:
		root = self._benches_root()
		return (root / bench_name).resolve()

	def _benches_root(self) -> Path:
		return Path(frappe.conf.get("release_group_script_benches_root") or DEFAULT_BENCHES_ROOT).expanduser()

	def _is_loadable_bench(self, bench_path: Path) -> bool:
		sites_path = bench_path / "sites"
		return bench_path.is_dir() and sites_path.is_dir()

	def _build_result_payload(self) -> str:
		rows = []
		for row in self.bench_runs:
			rows.append(
				{
					"bench": row.bench,
					"status": row.status,
					"skip_reason": row.skip_reason or "",
					"sites": row.sites or "[]",
					"stdout": row.stdout or "",
					"stderr": row.stderr or "",
					"exit_code": row.exit_code if row.exit_code is not None else "",
					"timed_out": int(row.timed_out or 0),
					"error": row.error or "",
				}
			)

		buffer = io.StringIO()
		writer = csv.DictWriter(
			buffer,
			fieldnames=[
				"bench",
				"status",
				"skip_reason",
				"sites",
				"stdout",
				"stderr",
				"exit_code",
				"timed_out",
				"error",
			],
		)
		writer.writeheader()
		writer.writerows(rows)
		return base64.b64encode(buffer.getvalue().encode("utf-8")).decode("ascii")

	def publish_update(self):
		frappe.publish_realtime(
			"release_group_script_run_update",
			doctype=self.doctype,
			docname=self.name,
			message=self.detail(),
		)

	def detail(self):
		return {
			"job": self.name,
			"status": self.status,
			"team": self.team,
			"requested_benches": self.requested_benches_list(),
			"timeout": self.timeout,
			"start": self.start,
			"end": self.end,
			"duration": self.duration,
			"result_format": "base64-csv",
			"result_payload": self.result_payload,
			"result": self.result_payload,
			"benches": [
				{
					"bench": row.bench,
					"status": row.status,
					"skip_reason": row.skip_reason,
					"sites": self._safe_json_loads(row.sites),
					"stdout": row.stdout,
					"stderr": row.stderr,
					"exit_code": row.exit_code,
					"timed_out": bool(row.timed_out),
					"error": row.error,
				}
				for row in self.bench_runs
			],
		}

	@staticmethod
	def get_detail(job_id):
		job = frappe.get_doc("Release Group Script Run", job_id)
		if job.team != get_current_team():
			frappe.throw("Not Permitted", frappe.PermissionError)
		return job.detail()

	@classmethod
	def create(cls, requested_benches: list[str], raw_script: str, timeout=None):
		team = get_current_team(get_doc=True)
		benches = cls._validate_requested_benches_for_team(team.name, requested_benches)
		doc = frappe.get_doc(
			{
				"doctype": "Release Group Script Run",
				"team": team.name,
				"requested_benches": benches,
				"raw_script": raw_script,
				"timeout": timeout or DEFAULT_TIMEOUT,
				"status": "Pending",
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	@staticmethod
	def _validate_requested_benches_for_team(team: str, requested_benches: list[str]) -> list[str]:
		allowed = set(
			frappe.get_all(
				"Bench",
				filters={"name": ("in", requested_benches), "team": team},
				pluck="name",
			)
		)
		missing = [bench for bench in requested_benches if bench not in allowed]
		if missing:
			frappe.throw("Not Permitted", frappe.PermissionError)
		return requested_benches

	@staticmethod
	def _clamp_timeout(value) -> int:
		timeout = cint(value or DEFAULT_TIMEOUT)
		if timeout < MIN_TIMEOUT:
			return MIN_TIMEOUT
		if timeout > MAX_TIMEOUT:
			return MAX_TIMEOUT
		return timeout

	@staticmethod
	def _coerce_text(value) -> str:
		if value is None:
			return ""
		if isinstance(value, bytes):
			return value.decode("utf-8", errors="replace")
		return str(value)

	@staticmethod
	def _safe_json_loads(value):
		if not value:
			return []
		if isinstance(value, str):
			with suppress(Exception):
				return json.loads(value)
		return value


from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from .release_group_script_run_bench.release_group_script_run_bench import (
		ReleaseGroupScriptRunBench,
	)
