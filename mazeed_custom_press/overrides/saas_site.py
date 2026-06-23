import frappe

from press.agent import Agent
from press.press.doctype.site.saas_site import SaasSite
from press.utils import log_error
from press.utils.dns import create_dns_record


class CustomSaasSite(SaasSite):
	"""Mazeed extension for Press SaasSite."""

	def update_configuration(self, config=None, save: bool = True):
		config = self._normalize_config(config)
		return self._update_configuration(config, save=save)

	def rename_pooled_site(self, account_request=None, subdomain=None, config=None):
		"""Rename a pooled site and carry any config payload into the rename job."""
		self._pending_rename_config = self._normalize_config(config)
		try:
			return super().rename_pooled_site(account_request=account_request, subdomain=subdomain)
		finally:
			self._pending_rename_config = None

	def rename(self, new_name: str):
		config = getattr(self, "_pending_rename_config", None)
		self.check_duplicate_site()
		create_dns_record(doc=self, record_name=self._get_site_name(self.subdomain))
		agent = Agent(self.server)
		if config:
			agent.rename_site(self, new_name, config=config)
		else:
			agent.rename_site(self, new_name)
		self.rename_upstream(new_name)
		self.status = "Pending"
		self.save()

		try:
			# remove old dns record from route53 after rename
			proxy_server = frappe.get_value("Server", self.server, "proxy_server")
			self.remove_dns_record(proxy_server)
		except Exception:
			log_error("Removing Old Site from Route53 Failed")

	def prefill_setup_wizard(self, system_settings_payload: dict, user_payload: dict):
		"""Override Press's prefill_setup_wizard to use sid-based auth.

		The parent uses get_connection_as_admin() which POSTs to /?cmd=login (legacy
		path). On a standby site whose setup wizard hasn't run yet, Frappe returns an
		empty body there, causing a JSONDecodeError. get_login_sid() uses the modern
		/api/method/login path plus an agent-side fallback, which works on new sites.
		"""
		if self.setup_wizard_complete or not system_settings_payload or not user_payload:
			return

		from frappe.frappeclient import FrappeClient

		sid = self.get_login_sid()
		conn = FrappeClient(f"https://{self.name}?sid={sid}")
		conn.post_api(
			"frappe.desk.page.setup_wizard.setup_wizard.initialize_system_settings_and_user",
			{"system_settings_data": system_settings_payload, "user_data": user_payload},
		)
		self.db_set("additional_system_user_created", 1)

	def _normalize_config(self, config) -> dict:
		"""Accept dict/list/json-string config payloads and normalize to dict."""
		if not config:
			return {}

		config = frappe.parse_json(config) if isinstance(config, str) else config

		if isinstance(config, dict):
			return config

		if isinstance(config, list):
			normalized = {}
			for row in config:
				if not isinstance(row, dict) or "key" not in row:
					continue
				normalized[row["key"]] = row.get("value")
			return normalized

		frappe.throw("Invalid config format. Expected dict or list of {key, value}.")

	# def set_pause_scheduler(self, value: int = 1):
	# 	self.update_config({"key": "pause_scheduler", "value": value, "type": "Number"})
	# 	return self

	# def set_default_team(self):
	# 	if self.app:
	# 		self.team = get_default_team_for_app(self.app)
	# 	return self

	# def apply_mazeed_defaults(self):
	# 	self.set_pause_scheduler()
	# 	self.set_default_team()
	# 	return self


def apply_overrides():
	"""Patch Press SaasSite and Site classes at runtime for current worker/process."""
	import press.press.doctype.site.saas_site as saas_site_module
	import press.press.doctype.site.site as site_module

	if saas_site_module.SaasSite is not CustomSaasSite:
		saas_site_module.SaasSite = CustomSaasSite

	if site_module.Site.prefill_setup_wizard is not CustomSaasSite.prefill_setup_wizard:
		site_module.Site.prefill_setup_wizard = CustomSaasSite.prefill_setup_wizard
