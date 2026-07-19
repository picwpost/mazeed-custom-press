from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


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
			"fieldname": "rollout_canary_size",
			"label": "Rollout Canary Size",
			"fieldtype": "Int",
			"default": "2",
			"description": "Default number of canary sites that must all succeed before the rest of the release group starts. 0 skips the canary gate.",
			"insert_after": "rollout_max_concurrent_updates",
			"depends_on": "eval:doc.enable_release_rollout_queue",
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
	if frappe.db.table_exists("Release Rollout Site"):
		frappe.db.add_unique("Release Rollout Site", ["rollout", "site"])
		frappe.db.add_index("Release Rollout Site", ["rollout", "status"])
		frappe.db.add_index("Release Rollout Site", ["site_update"])
