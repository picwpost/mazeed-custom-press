import frappe

from press.press.doctype.site.saas_site import SaasSite


class CustomSaasSite(SaasSite):
	"""Mazeed extension for Press SaasSite."""

	def update_configuration(self, config=None, save: bool = True):
		config = self._normalize_config(config)
		return self._update_configuration(config, save=save)

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
