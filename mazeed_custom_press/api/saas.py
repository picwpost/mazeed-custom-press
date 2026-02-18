import frappe

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

	pooled_site = get_pooled_saas_site(app)
	if pooled_site:
		site = CustomSaasSite(site=pooled_site, app=app).rename_pooled_site(
			subdomain=subdomain
		)
	else:
		site = CustomSaasSite(app=app, subdomain=subdomain).insert(
			ignore_permissions=True
		)
		site.create_subscription(get_saas_site_plan(app))

	config_payload = _normalize_site_config_payload(config)
	if config_payload:
		site.update_site_config(config_payload)
		site.reload()

	frappe.db.commit()

	return site
