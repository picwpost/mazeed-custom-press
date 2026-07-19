"""Shared fixtures for Release Rollout tests.

Two kinds of fixtures:

- ``fabricate_*`` insert bare rows with raw SQL. They are fast and are enough
  for code that only reads these doctypes with ``frappe.get_all`` /
  ``frappe.db.get_value`` (controller, reconciliation, canary, dashboard).
- ``create_updateable_site_environment`` builds a real Press environment with
  the official Press test factories so ``Site.schedule_update()`` works. Use
  it only where a real Site Update is required (starter, observer,
  characterization tests); it is slow.
"""

from __future__ import annotations

from types import SimpleNamespace

import frappe
from frappe.utils import now_datetime


def fabricate(doctype: str, name: str | None = None, **fields) -> str:
	name = name or f"test-rollout-{frappe.generate_hash(length=12)}"
	now = now_datetime()
	row = {
		"name": name,
		"creation": now,
		"modified": now,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
		**fields,
	}
	columns = ", ".join(f"`{column}`" for column in row)
	placeholders = ", ".join(["%s"] * len(row))
	frappe.db.sql(
		f"INSERT INTO `tab{doctype}` ({columns}) VALUES ({placeholders})",
		list(row.values()),
	)
	return name


def fabricate_release_group(team: str | None = None) -> str:
	return fabricate("Release Group", title=f"Rollout Test Group {frappe.generate_hash(length=6)}", team=team)


def fabricate_bench(group: str, status: str = "Active") -> str:
	return fabricate("Bench", group=group, status=status)


def fabricate_site(bench: str, status: str = "Active") -> str:
	return fabricate("Site", bench=bench, status=status)


def fabricate_site_update(site: str, status: str = "Pending", **fields) -> str:
	return fabricate("Site Update", site=site, status=status, **fields)


def make_rollout(
	release_group: str,
	max_concurrent_updates: int = 2,
	canary_size: int = 0,
	status: str = "Running",
	stage: str = "Main",
	canary_status: str = "Passed",
	total_sites: int = 0,
	**fields,
):
	return frappe.get_doc(
		{
			"doctype": "Release Rollout",
			"release_group": release_group,
			"status": status,
			"stage": stage,
			"max_concurrent_updates": max_concurrent_updates,
			"canary_size": canary_size,
			"canary_status": canary_status,
			"total_sites": total_sites,
			"started_at": now_datetime(),
			"started_by": "Administrator",
			**fields,
		}
	).insert(ignore_permissions=True)


def make_rollout_site(
	rollout: str,
	site: str,
	source_bench: str,
	status: str = "Pending",
	is_canary: int = 0,
	**fields,
):
	return frappe.get_doc(
		{
			"doctype": "Release Rollout Site",
			"rollout": rollout,
			"site": site,
			"source_bench": source_bench,
			"status": status,
			"is_canary": is_canary,
			**fields,
		}
	).insert(ignore_permissions=True)


def backdate_modified(doctype: str, name: str, minutes: int):
	frappe.db.sql(
		f"UPDATE `tab{doctype}` SET modified = DATE_SUB(NOW(), INTERVAL %s MINUTE) WHERE name = %s",
		(minutes, name),
	)


def create_updateable_site_environment() -> SimpleNamespace:
	"""Real Press environment where ``Site.schedule_update()`` finds a destination."""
	from press.press.doctype.app.test_app import create_test_app
	from press.press.doctype.app_release.test_app_release import create_test_app_release
	from press.press.doctype.app_source.test_app_source import create_test_app_source
	from press.press.doctype.deploy_candidate_difference.test_deploy_candidate_difference import (
		create_test_deploy_candidate_differences,
	)
	from press.press.doctype.release_group.test_release_group import create_test_release_group
	from press.press.doctype.site.test_site import create_test_bench, create_test_site

	from press.press.doctype.site_update.site_update import benches_with_available_update

	# In-process @site_cache survives the per-test rollback; a stale entry from
	# an earlier test would make destination resolution fail for this fresh env.
	benches_with_available_update.clear_cache()

	version = "Version 13"
	app = create_test_app()
	app_source = create_test_app_source(version=version, app=app)
	group = create_test_release_group([app], frappe_version=version)
	bench1 = create_test_bench(group=group)
	create_test_app_release(app_source=app_source)
	bench2 = create_test_bench(group=group, server=bench1.server)
	create_test_deploy_candidate_differences(bench2.candidate)
	site = create_test_site(bench=bench1.name)
	return SimpleNamespace(app=app, group=group, bench1=bench1, bench2=bench2, site=site)


def make_website_user() -> str:
	# Press access checks use helpers that press.tests.before_test.execute puts
	# on frappe.local; make sure they exist when permission tests run alone.
	from press.utils import _get_current_team, _system_user

	frappe.local.team = _get_current_team
	frappe.local.system_user = _system_user

	email = f"rollout-outsider-{frappe.generate_hash(length=8)}@example.com"
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Rollout",
			"last_name": "Outsider",
			"user_type": "Website User",
		}
	)
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)
	return email
