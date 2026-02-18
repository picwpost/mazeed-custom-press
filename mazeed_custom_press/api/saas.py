import frappe

from press.press.doctype.site.saas_pool import get as get_pooled_saas_site
from press.press.doctype.site.saas_site import get_saas_site_plan

from mazeed_custom_press.overrides.saas_site import CustomSaasSite


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

	site.update_configuration(config or {}, save=False)
	site.reload()
	site.save(ignore_permissions=True)

	frappe.db.commit()

	return site
