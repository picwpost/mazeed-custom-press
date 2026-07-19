import frappe
from frappe.model.naming import make_autoname

from press.press.doctype.site.saas_site import (
	create_app_subscriptions,
	get_pool_apps,
	get_saas_apps,
	get_saas_bench,
	get_saas_domain,
	set_site_in_subscription_docs,
)
from press.utils import log_error


def _send_pool_creation_failure_email(app, domain, subdomain, bench, apps, error):
	frappe.sendmail(
		recipients=["ahmed.abdellatif@mazeed.com"],
		subject=f"[Mazeed Press] Pool Site Creation Failed for {app}",
		message=(
			"Pool site creation failed.<br><br>"
			f"<b>App:</b> {frappe.as_unicode(app)}<br>"
			f"<b>Domain:</b> {frappe.as_unicode(domain)}<br>"
			f"<b>Subdomain:</b> {frappe.as_unicode(subdomain)}<br>"
			f"<b>Bench:</b> {frappe.as_unicode(bench)}<br>"
			f"<b>Apps:</b> {frappe.as_unicode(apps)}<br>"
			f"<b>Error:</b> {frappe.as_unicode(error)}"
		),
		 delayed=False,
	)

def custom_create_one(self, pool_name: str = ""):
	"""Override for press.press.doctype.site.saas_pool.SaasSitePool.create_one."""
	bench, apps, subdomain, domain = None, None, None, None
	try:
		domain = get_saas_domain(self.app)
		bench = get_saas_bench(self.app)
		apps = get_saas_apps(self.app)
		if pool_name:
			apps.extend(get_pool_apps(pool_name))
		for _ in range(5):
			subdomain = self.get_subdomain()
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
			site._update_configuration({"pause_scheduler": 1}, save=False)
			subscription_docs = create_app_subscriptions(site, self.app)
			try:
				site.insert()
				set_site_in_subscription_docs(subscription_docs, site.name)
				break
			except frappe.DuplicateEntryError:
				frappe.db.rollback()
				continue
		else:
			raise frappe.DuplicateEntryError(
				"Site", f"Could not create unique standby site for app {self.app} after 5 attempts"
			)
	except Exception:
		log_error(
			"Pool Site Creation Error",
			domain=domain,
			subdomain=subdomain,
			bench=bench,
			apps=apps,
		)
		frappe.log_error(frappe.get_traceback(), "Pool Site Creation Traceback")
		_send_pool_creation_failure_email(
			app=self.app,
			domain=domain,
			subdomain=subdomain,
			bench=bench,
			apps=apps,
			error=frappe.get_traceback(),
		)
		raise


def custom_get_subdomain(self):
	"""Override for press.press.doctype.site.saas_pool.SaasSitePool.get_subdomain."""
	return make_autoname("workspace-.########")


def custom_get(self, hybrid_saas_pool):
	"""Override for press.press.doctype.site.saas_pool.SaasSitePool.get."""
	filters = {
		"is_standby": True,
		"standby_for": self.app,
		"status": "Active",
		"name": ("like", "workspace%"),
	}

	if hybrid_saas_pool:
		filters.update({"hybrid_saas_pool": hybrid_saas_pool})
	else:
		filters.update({"hybrid_saas_pool": ("is", "not set")})

	sites = frappe.get_all("Site", filters, pluck="name", order_by="creation", limit=1)

	return sites[0] if sites else sites


def apply_overrides():
	"""Patch Press methods at runtime for current worker/process."""
	from press.press.doctype.site.saas_pool import SaasSitePool

	if SaasSitePool.create_one is not custom_create_one:
		SaasSitePool.create_one = custom_create_one
	if SaasSitePool.get_subdomain is not custom_get_subdomain:
		SaasSitePool.get_subdomain = custom_get_subdomain
	if SaasSitePool.get is not custom_get:
		SaasSitePool.get = custom_get
