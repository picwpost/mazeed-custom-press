from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from mazeed_custom_press import agent_job_types


CUSTOM_FIELDS = {
	"Press Settings": [
		{
			"fieldname": "enable_release_rollout_queue",
			"label": "Enable Release Rollout Queue",
			"fieldtype": "Check",
			"default": "0",
			"description": "Route Release Group Update All Sites through the rolling queue.",
			"insert_after": "auto_update_queue_size",
		},
		{
			"fieldname": "rollout_max_concurrent_updates",
			"label": "Rollout Max Concurrent Updates",
			"fieldtype": "Int",
			"default": "2",
			"description": "Default number of sites updating in parallel per rollout. Captured when a rollout is created; changing it never affects a running rollout.",
			"insert_after": "enable_release_rollout_queue",
			"depends_on": "eval:doc.enable_release_rollout_queue",
		},
		{
			"fieldname": "rollout_max_concurrent_updates_per_server",
			"label": "Rollout Max Concurrent Updates Per Server",
			"fieldtype": "Int",
			# 0, not 2: a non-zero default would silently cap every existing
			# rollout that raised the total, and since most release groups sit
			# on one server that would cut throughput rather than raise it.
			"default": "0",
			"description": (
				"Cap on sites updating at once on any single server. Agent workers are per "
				"server while Rollout Max Concurrent Updates is a total across the rollout, so "
				"without this a release group spread over several servers leaves most of them "
				"idle. 0 means no separate per-server cap -- the rollout total is used."
			),
			"insert_after": "rollout_max_concurrent_updates",
			"depends_on": "eval:doc.enable_release_rollout_queue",
		},
		{
			"fieldname": "rollout_canary_size",
			"label": "Rollout Canary Size",
			"fieldtype": "Int",
			"default": "2",
			"description": "Default number of canary sites that must all succeed before the rest of the release group starts. 0 skips the canary gate.",
			"insert_after": "rollout_max_concurrent_updates_per_server",
			"depends_on": "eval:doc.enable_release_rollout_queue",
		},
		{
			"fieldname": "rollout_skip_backups_for_main_stage",
			"label": "Rollout: Skip Backups For Main Stage",
			"fieldtype": "Check",
			"default": "0",
			"description": (
				"Skip the pre-migrate backup for main-stage sites in a rollout, once the canary "
				"has already proven the update safe. Canary sites always still get a backup. This "
				"only skips the one-off backup taken immediately before this update -- the regular "
				"scheduled site backup system is entirely separate and is never affected."
			),
			"insert_after": "rollout_canary_size",
			"depends_on": "eval:doc.enable_release_rollout_queue",
		},
	],
	"Site": [
		{
			"fieldname": "use_as_rollout_canary",
			"label": "Use as Rollout Canary",
			"fieldtype": "Check",
			"default": "0",
			"description": (
				"Update this site first in every rollout, and hold the rest back until it "
				"succeeds. Use it for sites you are willing to break -- internal workspaces, "
				"staging tenants. The flag is a standing preference, not a per-deploy "
				"instruction: if the site is later archived or moved out of the group being "
				"rolled out, it is simply not picked, and the rollout still runs."
			),
			"insert_after": "status",
		},
	],
	"Site Update": [
		{
			"fieldname": "release_rollout_site",
			"label": "Release Rollout Site",
			"fieldtype": "Link",
			"options": "Release Rollout Site",
			"read_only": 1,
			"no_copy": 1,
			"unique": 1,
		}
	],
}


def after_install():
	if "press" not in frappe.get_installed_apps():
		frappe.throw("Mazeed Custom Press requires the Press app to be installed first.")
	after_migrate()


def after_migrate():
	# ignore_validate skips revalidating the whole target DocType; Press ships
	# metadata quirks (e.g. duplicate fieldnames on Press Settings) that would
	# otherwise abort installation of these unrelated fields.
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)
	# Must run after Press's sync_fixtures(), which re-imports and overwrites
	# every Agent Job Type it defines. frappe runs after_migrate hooks last, so
	# this re-applies our steps on top of the freshly synced fixtures.
	agent_job_types.sync()
	if frappe.db.table_exists("Release Rollout Site"):
		frappe.db.add_unique("Release Rollout Site", ["rollout", "site"])
		frappe.db.add_index("Release Rollout Site", ["rollout", "status"])
		frappe.db.add_index("Release Rollout Site", ["site_update"])
