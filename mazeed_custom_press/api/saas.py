import frappe

from press.press.doctype.site.saas_pool import get as get_pooled_saas_site
from press.press.doctype.site.saas_site import SaasSite, get_default_team_for_app, get_saas_site_plan


@frappe.whitelist()
def new_saas_site(subdomain, app):
	"""Override for press.press.api.saas.new_saas_site."""
	frappe.only_for("System Manager")

	pooled_site = get_pooled_saas_site(app)
	if pooled_site:
		site = SaasSite(site=pooled_site, app=app).rename_pooled_site(subdomain=subdomain)
	else:
		site = SaasSite(app=app, subdomain=subdomain).insert(ignore_permissions=True)
		site.create_subscription(get_saas_site_plan(app))

	site.update_config({
		"key": "pause_scheduler",
		"value": 1,
		"type": "Number"
		})
	site.reload()
	site.team = get_default_team_for_app(app)
	site.save(ignore_permissions=True)

	frappe.db.commit()

	return site
