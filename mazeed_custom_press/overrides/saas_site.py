import contextlib
import json

import frappe

from press.agent import Agent
from press.press.doctype.site.saas_site import SaasSite, get_saas_site_plan
from press.utils import log_error
from press.utils.dns import create_dns_record


class CustomSaasSite(SaasSite):
	"""Mazeed extension for Press SaasSite."""

	def update_configuration(self, config=None, save: bool = True):
		config = self._normalize_config(config)
		return self._update_configuration(config, save=save)

	def rename_pooled_site(self, account_request=None, subdomain=None, config=None):
		"""Rename a pooled site and carry any config payload into the rename job."""
		if self.app == "erpnext":
			return self._rename_pooled_site_erpnext(account_request=account_request, config=config)

		# mazeed_theme (and any future app): original behaviour — Phase 1 + Phase 2
		self._pending_rename_config = self._normalize_config(config)
		try:
			return super().rename_pooled_site(account_request=account_request, subdomain=subdomain)
		finally:
			self._pending_rename_config = None

	def _rename_pooled_site_erpnext(self, account_request=None, config=None):
		"""Phase 1 only: update site metadata but keep the standby subdomain unchanged.
		No subdomain change → Site lifecycle does not trigger rename() or the agent job."""
		self.is_standby = False
		self.account_request = account_request.name if account_request else ""
		self.trial_end_date = frappe.utils.add_days(None, 14)
		plan = get_saas_site_plan(self.app)
		self._update_configuration(self.get_plan_config(plan), save=False)
		subscription_config = {}
		for row in self.configuration:
			if row.key == "subscription":
				with contextlib.suppress(json.JSONDecodeError):
					subscription_config = json.loads(row.value)
		subscription_config["trial_end_date"] = self.trial_end_date.strftime("%Y-%m-%d")
		self._update_configuration({"subscription": subscription_config}, save=False)
		if config:
			self._update_configuration(self._normalize_config(config), save=False)
		self.save(ignore_permissions=True)
		self.create_subscription(plan)
		self.reload()
		return self

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
	"""Patch Press SaasSite class at runtime for current worker/process."""
	import press.press.doctype.site.saas_site as saas_site_module

	if saas_site_module.SaasSite is not CustomSaasSite:
		saas_site_module.SaasSite = CustomSaasSite
