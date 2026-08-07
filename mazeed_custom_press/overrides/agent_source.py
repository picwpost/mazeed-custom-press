from __future__ import annotations

import frappe

_original_get_agent_repository_url = None
_original_get_agent_repository_branch = None


def _cluster_agent_override(self, fieldname):
	"""Read a custom_agent_* field off this server's Cluster, if set."""
	if not self.cluster:
		return None
	return frappe.db.get_value("Cluster", self.cluster, fieldname)


def custom_get_agent_repository_url(self):
	"""Override for press.press.doctype.server.server.BaseServer.get_agent_repository_url.

	Lets a single Cluster (e.g. a development cluster validating agent changes)
	run a different agent fork, without affecting any other cluster's servers,
	which keep resolving Press Settings' global agent_repository_owner exactly
	as before.
	"""
	owner = _cluster_agent_override(self, "custom_agent_repository_owner")
	if owner:
		return f"https://github.com/{owner}/agent"
	return _original_get_agent_repository_url(self)


def custom_get_agent_repository_branch(self):
	"""Override for press.press.doctype.server.server.BaseServer.get_agent_repository_branch.

	Same per-Cluster override as custom_get_agent_repository_url, for the branch.
	"""
	branch = _cluster_agent_override(self, "custom_agent_branch")
	if branch:
		return branch
	return _original_get_agent_repository_branch(self)


def apply_overrides():
	"""Patch Press's BaseServer agent-source methods at runtime for current worker/process.

	Patching BaseServer covers both Server and Proxy Server (both subclass it
	and neither overrides these two methods), so a single patch here is enough
	for both doctypes.
	"""
	global _original_get_agent_repository_url, _original_get_agent_repository_branch
	from press.press.doctype.server.server import BaseServer

	if BaseServer.get_agent_repository_url is not custom_get_agent_repository_url:
		if _original_get_agent_repository_url is None:
			_original_get_agent_repository_url = BaseServer.get_agent_repository_url
		BaseServer.get_agent_repository_url = custom_get_agent_repository_url

	if BaseServer.get_agent_repository_branch is not custom_get_agent_repository_branch:
		if _original_get_agent_repository_branch is None:
			_original_get_agent_repository_branch = BaseServer.get_agent_repository_branch
		BaseServer.get_agent_repository_branch = custom_get_agent_repository_branch
