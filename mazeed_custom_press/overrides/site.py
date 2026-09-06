import frappe
import requests
from frappe.utils.password import get_decrypted_password

from press.utils import log_error

_original_get_login_sid = None


def _get_sid_via_mobile_login(site, user):
	"""Try to obtain a sid from the site's own mazeed_theme mobile login API."""
	try:
		password = get_decrypted_password("Site", site.name, "admin_password")
		response = requests.post(
			f"https://{site.name}/api/method/mazeed_theme.api.mobile.mobile_login",
			data={"usr": user, "pwd": password},
			timeout=(5, 15),
		)
		response.raise_for_status()
		sid = response.cookies.get("sid")
		if sid and sid != "Guest":
			return sid
	except Exception:
		frappe.log_error(title="Mobile Login Sid Fetch Failed")
	return None


def custom_get_login_sid(self, user: str = "Administrator"):
	"""Override for press.press.doctype.site.site.Site.get_login_sid.

	Tries mazeed_theme's mobile_login endpoint on the site first; on any
	failure, falls back to Press's original get_login_sid flow unchanged.
	"""
	sid = _get_sid_via_mobile_login(self, user)
	if sid:
		return sid
	return _original_get_login_sid(self, user)


def custom_validate_installed_apps(self):
	"""Override for press.press.doctype.site.site.Site.validate_installed_apps.

	Skips (and logs) any app not available on the target Bench instead of
	throwing and aborting site creation.
	"""
	bench_apps = frappe.get_doc("Bench", self.bench).apps
	bench_app_names = [app.app for app in bench_apps]

	valid_apps = []
	for app in self.apps:
		if app.app not in bench_app_names:
			log_error(
				"Site App Not On Bench - Skipped",
				site=self.name,
				bench=self.bench,
				app=app.app,
			)
			continue
		valid_apps.append(app)
	self.apps = valid_apps

	if not self.apps or self.apps[0].app != "frappe":
		frappe.throw("First app to be installed on site must be frappe.")

	site_apps = [app.app for app in self.apps]
	if len(site_apps) != len(set(site_apps)):
		frappe.throw("Can't install same app twice.")

	if self.is_new():
		self.sort_apps(bench_apps)


def apply_overrides():
	"""Patch Press's Site methods at runtime for current worker/process."""
	global _original_get_login_sid
	from press.press.doctype.site.site import Site

	if Site.get_login_sid is not custom_get_login_sid:
		if _original_get_login_sid is None:
			_original_get_login_sid = Site.get_login_sid
		Site.get_login_sid = custom_get_login_sid

	if Site.validate_installed_apps is not custom_validate_installed_apps:
		Site.validate_installed_apps = custom_validate_installed_apps
