import frappe

from press.press.doctype.site.saas_site import (
	create_app_subscriptions,
	get_pool_apps,
	get_saas_apps,
	get_saas_bench,
	get_saas_domain,
	set_site_in_subscription_docs,
)
from press.utils import log_error


def custom_create_one(self, pool_name: str = ""):
	"""Override for press.press.doctype.site.saas_pool.SaasSitePool.create_one."""
	bench, apps, subdomain, domain = None, None, None, None
	try:
		domain = get_saas_domain(self.app)
		bench = get_saas_bench(self.app)
		subdomain = self.get_subdomain()
		apps = get_saas_apps(self.app)
		if pool_name:
			apps.extend(get_pool_apps(pool_name))
		site = frappe.get_doc(
			{
				"doctype": "Site",
				"subdomain": subdomain,
				"domain": domain,
				"is_standby": True,
				"standby_for": self.app,
				"hybrid_saas_pool": pool_name,
				"team": frappe.get_value("Team", {"user": "Administrator"}, "name"),
				"bench": bench,
				"apps": [{"app": app} for app in apps],
			}
		)
		site.update_site_config({"pause_scheduler": 1})
		subscription_docs = create_app_subscriptions(site, self.app)
		site.insert()
		set_site_in_subscription_docs(subscription_docs, site.name)
	except Exception:
		log_error(
			"Pool Site Creation Error",
			domain=domain,
			subdomain=subdomain,
			bench=bench,
			apps=apps,
		)
		raise


def apply_overrides():
	"""Patch Press methods at runtime for current worker/process."""
	from press.press.doctype.site.saas_pool import SaasSitePool

	if SaasSitePool.create_one is not custom_create_one:
		SaasSitePool.create_one = custom_create_one
