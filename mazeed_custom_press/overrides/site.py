import frappe
import requests
from frappe.utils.password import get_decrypted_password

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


def apply_overrides():
	"""Patch Press's Site.get_login_sid at runtime for current worker/process."""
	global _original_get_login_sid
	from press.press.doctype.site.site import Site

	if Site.get_login_sid is not custom_get_login_sid:
		if _original_get_login_sid is None:
			_original_get_login_sid = Site.get_login_sid
		Site.get_login_sid = custom_get_login_sid
