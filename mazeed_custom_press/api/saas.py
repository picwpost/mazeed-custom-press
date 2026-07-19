import json

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
def send_setup_wizard_to_standby_site(release_group, args, config=None):
	"""
	Fetch the first ready standby site for a Release Group, optionally update its
	site config, then run its full Setup Wizard.

	args: {
	    "language": "English", "country": "...", "timezone": "...", "currency": "...",
	    "full_name": "...", "email": "...", "password": "...",
	    "company_name": "...", "company_abbr": "...", "domain": "...",
	    "chart_of_accounts": "Standard", "usage_goal": "...",
	    "fy_start_date": "YYYY-MM-DD", "fy_end_date": "YYYY-MM-DD"
	}
	config (optional): site config dict to apply before the wizard runs
	"""
	frappe.only_for("System Manager")

	args = frappe.parse_json(args) if isinstance(args, str) else args
	config_payload = frappe.parse_json(config) if isinstance(config, str) else config

	site_info = get_standby_site_for_release_group(release_group)
	site_name = site_info["name"]

	logger = frappe.logger("mazeed_custom_press.api.saas", with_more_info=True)

	site = frappe.get_doc("Site", site_name)

	config_update_result = None
	if config_payload:
		from press.agent import Agent

		logger.info(f"[send_setup_wizard] updating config for {site_name}: {frappe.as_json(config_payload)}")
		try:
			# Merge into Press DB (preserves existing keys, does not replace)
			config_dict = {
				item["key"]: item["value"]
				for item in config_payload
				if isinstance(item, dict) and item.get("key") and item.get("value") is not None
			}
			site._update_configuration(config_dict)
			# After save(), site.config is refreshed by validate_site_config() in memory
			logger.info(f"[send_setup_wizard] Press DB updated, site.config={site.config}")

			# Push synchronously — create_agent_job only queues a DB record (Undelivered);
			# the scheduler delivers it later, which is too late for the wizard to see the config.
			agent = Agent(site.server)
			config_update_result = agent.post(
				f"benches/{site.bench}/sites/{site.name}/config",
				data={
					"config": json.loads(site.config),
					"remove": json.loads(site._keys_removed_in_last_update or "[]"),
				},
			)
			logger.info(f"[send_setup_wizard] agent push result for {site_name}: {config_update_result}")
		except Exception as e:
			logger.error(f"[send_setup_wizard] config update failed for {site_name}: {e}")
			frappe.throw(f"Site config update failed for '{site_name}': {e}")

	if not site.setup_wizard_complete:
		logger.info(f"[send_setup_wizard] getting login sid for {site_name}")
		try:
			sid = site.get_login_sid()
			logger.info(f"[send_setup_wizard] got sid for {site_name}, calling setup_complete")
			response = requests.post(
				f"https://{site_name}/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
				data={"args": frappe.as_json(args)},
				cookies={"sid": sid},
				timeout=120,
			)
			response.raise_for_status()
			result = response.json()
			logger.info(f"[send_setup_wizard] setup_complete response for {site_name}: {result}")
			if result.get("message", {}).get("status") not in ("ok", "registered"):
				frappe.throw(f"Setup wizard failed for '{site_name}': {result}")
		except requests.exceptions.RequestException as e:
			logger.error(f"[send_setup_wizard] HTTP error for {site_name}: {e}")
			frappe.throw(f"Could not connect to site '{site_name}' to run the setup wizard: {e}")

	site.db_set("setup_wizard_complete", 1)

	return {
		"site": site_name,
		"bench": site_info["bench"],
		"config_update": config_update_result,
	}
