from frappe.model.naming import make_autoname


def custom_get_subdomain(self):
	"""Override for press.press.doctype.site.pool.SitePool.get_subdomain."""
	return make_autoname("workspace-.########")


def apply_overrides():
	"""Patch Press's SitePool.get_subdomain at runtime for current worker/process."""
	from press.press.doctype.site.pool import SitePool

	if SitePool.get_subdomain is not custom_get_subdomain:
		SitePool.get_subdomain = custom_get_subdomain
