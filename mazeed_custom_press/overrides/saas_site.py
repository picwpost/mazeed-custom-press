import frappe

from press.press.doctype.site.saas_site import SaasSite, get_default_team_for_app


class CustomSaasSite(SaasSite):
	"""Mazeed extension for Press SaasSite."""

	def update_configuration(self, config: dict, save: bool = True):
		return self._update_configuration(config, save=save)

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
