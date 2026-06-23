import frappe
import requests

from press.press.doctype.site.saas_pool import get as get_pooled_saas_site
from press.press.doctype.site.saas_site import get_saas_site_plan

from mazeed_custom_press.overrides.saas_site import CustomSaasSite


def _normalize_site_config_payload(config):
	"""Normalize incoming config payload into a dict for merge-style updates."""
	if not config:
		return {}

	config = frappe.parse_json(config) if isinstance(config, str) else config

	if isinstance(config, dict):
		return config

	if isinstance(config, list):
		normalized = {}
		for row in config:
			if not isinstance(row, dict):
				continue
			# supports [{"key": "...", "value": ...}]
			if "key" in row:
				normalized[row["key"]] = row.get("value")
				continue
			# supports [{"k1": v1}, {"k2": v2}]
			normalized.update(row)
		return normalized

	frappe.throw("Invalid config format. Expected dict or list of dicts.")


@frappe.whitelist()
def new_saas_site(subdomain, app, config=None):
	"""Override for press.press.api.saas.new_saas_site."""
	frappe.only_for("System Manager")

	config_payload = _normalize_site_config_payload(config)

	if pooled_site := get_pooled_saas_site(app):
		site = CustomSaasSite(site=pooled_site, app=app).rename_pooled_site(
			subdomain=subdomain, config=config_payload
		)
	else:
		site = CustomSaasSite(app=app, subdomain=subdomain).insert(ignore_permissions=True)
		site.create_subscription(get_saas_site_plan(app))
		if config_payload:
			site.reload()
			site.update_site_config(config_payload)
			site.reload()

	frappe.db.commit()

	return site


@frappe.whitelist()
def get_standby_site_for_release_group(release_group):
	"""Return the first active standby site (setup_wizard_complete=0) on the latest active bench for a Release Group."""
	frappe.only_for("System Manager")

	rg_name = frappe.db.get_value("Release Group", release_group) or frappe.db.get_value(
		"Release Group", {"title": release_group}
	)
	if not rg_name:
		frappe.throw(f"Release Group '{release_group}' not found.")

	bench = frappe.db.get_value(
		"Bench",
		{"group": rg_name, "status": "Active"},
		"name",
		order_by="creation desc",
	)
	if not bench:
		frappe.throw(f"No active bench found for Release Group '{rg_name}'.")

	site = frappe.db.get_value(
		"Site",
		{
			"bench": bench,
			"status": "Active",
			"name": ("like", "standby%"),
			"setup_wizard_complete": 0,
		},
		["name", "bench", "status", "setup_wizard_complete"],
		as_dict=True,
		order_by="creation asc",
	)
	if not site:
		frappe.throw(f"No available standby site on bench '{bench}'.")

	return site


@frappe.whitelist()
def send_setup_wizard_to_standby_site(release_group, system_settings, user_settings):
	"""
	Fetch the first ready standby site for a Release Group and prefill its setup wizard.

	system_settings: {"country": ..., "time_zone": ..., "language": "en", "currency": ...}
	user_settings:   {"email": ..., "first_name": ..., "last_name": ..., "full_name": ...}
	"""
	frappe.only_for("System Manager")

	system_settings = frappe.parse_json(system_settings) if isinstance(system_settings, str) else system_settings
	user_settings = frappe.parse_json(user_settings) if isinstance(user_settings, str) else user_settings

	site_info = get_standby_site_for_release_group(release_group)
	site_name = site_info["name"]

	site = frappe.get_doc("Site", site_name)

	if not site.setup_wizard_complete:
		from frappe.frappeclient import FrappeClient

		try:
			sid = site.get_login_sid()
			conn = FrappeClient(f"https://{site.name}?sid={sid}")
			conn.post_api(
				"frappe.desk.page.setup_wizard.setup_wizard.initialize_system_settings_and_user",
				{"system_settings_data": system_settings, "user_data": user_settings},
			)
			site.db_set("additional_system_user_created", 1)
		except requests.exceptions.RequestException as e:
			frappe.throw(f"Could not connect to site '{site_name}' to run the setup wizard: {e}")

	site.db_set("setup_wizard_complete", 1)

	return {"site": site_name, "bench": site_info["bench"]}
