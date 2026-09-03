"""Throwaway script for reproducing the skip_backups report. Not a test module."""

from unittest.mock import Mock

import frappe


def setup():
	from press.press.doctype.agent_job.agent_job import AgentJob
	from press.press.doctype.site.test_site import create_test_site

	AgentJob.enqueue_http_request = Mock()

	from mazeed_custom_press.overrides.bench import apply_overrides as apply_bench_overrides
	from mazeed_custom_press.overrides.site_update import apply_overrides as apply_site_update_overrides
	from mazeed_custom_press.tests.rollout_test_utils import create_updateable_site_environment

	apply_bench_overrides()
	apply_site_update_overrides()

	# Enable it BEFORE creating the rollout, matching what the user reported doing.
	frappe.db.set_single_value("Press Settings", "enable_release_rollout_queue", 1)
	frappe.db.set_single_value("Press Settings", "rollout_skip_backups_for_main_stage", 1)
	frappe.db.set_single_value("Press Settings", "rollout_canary_size", 1)
	frappe.db.set_single_value("Press Settings", "rollout_max_concurrent_updates", 2)

	environment = create_updateable_site_environment()
	main_site = create_test_site(bench=environment.bench1.name)
	frappe.db.commit()

	bench_doc = frappe.get_doc("Bench", environment.bench1.name)
	bench_doc.update_all_sites()
	frappe.db.commit()

	return {
		"group": environment.group.name,
		"bench1": environment.bench1.name,
		"canary_site": environment.site.name,
		"main_site": main_site.name,
	}
