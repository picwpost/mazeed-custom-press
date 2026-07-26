from __future__ import annotations

import frappe

_original_update_status = None


def custom_update_status(name, status):
	"""Override for press.press.doctype.site_update.site_update.update_status.

	Press updates a Site Update's status via a raw `frappe.db.set_value` call
	(a deliberate hot-path optimization for the poller), which never fires
	`on_change` -- the `doc_events: {"Agent Job": {"on_change": ...}}` hook in
	hooks.py therefore never actually runs when a real update completes; only
	the 2-minute `reconcile_running_rollouts` safety net ever notices,
	which is why detection looked like it took minutes (DASH-19).

	This module-level function is the one place that runs synchronously,
	inside Press's own poll cycle, every single time a Site Update's status
	actually changes -- patching it is what gives us real, immediate sync.
	"""
	_original_update_status(name, status)

	from mazeed_custom_press.release_rollout import TERMINAL_SITE_UPDATE_MAP, logger

	if status not in TERMINAL_SITE_UPDATE_MAP:
		return
	if not frappe.db.exists("Release Rollout Site", {"site_update": name}):
		return
	logger.info(f"update_status override: site_update={name} status={status} -- syncing rollout immediately")
	frappe.enqueue(
		"mazeed_custom_press.release_rollout.sync_site_update",
		site_update_name=name,
		queue="short",
		enqueue_after_commit=True,
	)


def apply_overrides():
	"""Patch Press's site_update.update_status at runtime for current worker/process."""
	global _original_update_status
	from press.press.doctype.site_update import site_update as site_update_module

	if site_update_module.update_status is not custom_update_status:
		if _original_update_status is None:
			_original_update_status = site_update_module.update_status
		site_update_module.update_status = custom_update_status
