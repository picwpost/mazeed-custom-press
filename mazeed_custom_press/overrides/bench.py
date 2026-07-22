from __future__ import annotations

from press.api.client import dashboard_whitelist

from mazeed_custom_press.release_rollout import logger

_original_update_all_sites = None


@dashboard_whitelist()
def custom_update_all_sites(self):
	"""Override for press.press.doctype.bench.bench.Bench.update_all_sites.

	Both the dashboard's "Update All Sites" button and the Bench desk form's
	button reach this through Frappe's generic `run_doc_method`, which calls
	`getattr(doc, method)` directly -- it never consults
	`override_whitelisted_methods`, so patching that hook alone (as the
	`press.api.bench.update_all_sites` entry in hooks.py still does, for any
	caller hitting the dotted path directly) never actually runs for either
	button. Patching the instance method itself is the only way both entry
	points reach the rollout queue. Must stay `@dashboard_whitelist()` (not
	plain `@frappe.whitelist()`) so this replacement function is present in
	press.api.client.whitelisted_methods -- check_dashboard_actions() checks
	identity against that set, not just frappe's global whitelist registry.
	"""
	logger.info(f"bench.update_all_sites override invoked bench={self.name} group={self.group}")
	from mazeed_custom_press.api.release_rollout import update_all_sites

	update_all_sites(name=self.group)


def apply_overrides():
	"""Patch Press's Bench.update_all_sites at runtime for current worker/process."""
	global _original_update_all_sites
	from press.press.doctype.bench.bench import Bench

	if Bench.update_all_sites is not custom_update_all_sites:
		if _original_update_all_sites is None:
			_original_update_all_sites = Bench.update_all_sites
		Bench.update_all_sites = custom_update_all_sites
