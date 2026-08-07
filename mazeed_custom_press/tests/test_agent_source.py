from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.overrides import agent_source


class TestAgentSourceOverride(FrappeTestCase):
	def test_url_uses_cluster_override_when_set(self):
		server = Mock()
		server.cluster = "dev-cluster"

		with patch(
			"mazeed_custom_press.overrides.agent_source.frappe.db.get_value",
			return_value="my-fork",
		):
			url = agent_source.custom_get_agent_repository_url(server)

		self.assertEqual(url, "https://github.com/my-fork/agent")

	def test_url_falls_back_to_original_when_cluster_field_blank(self):
		server = Mock()
		server.cluster = "prod-cluster"

		original = Mock(return_value="https://github.com/frappe/agent")
		with (
			patch("mazeed_custom_press.overrides.agent_source.frappe.db.get_value", return_value=None),
			patch("mazeed_custom_press.overrides.agent_source._original_get_agent_repository_url", original),
		):
			url = agent_source.custom_get_agent_repository_url(server)

		original.assert_called_once_with(server)
		self.assertEqual(url, "https://github.com/frappe/agent")

	def test_branch_falls_back_when_server_has_no_cluster(self):
		server = Mock()
		server.cluster = None

		original = Mock(return_value="master")
		with patch(
			"mazeed_custom_press.overrides.agent_source._original_get_agent_repository_branch", original
		):
			branch = agent_source.custom_get_agent_repository_branch(server)

		original.assert_called_once_with(server)
		self.assertEqual(branch, "master")

	def test_branch_uses_cluster_override_when_set(self):
		server = Mock()
		server.cluster = "dev-cluster"

		with patch(
			"mazeed_custom_press.overrides.agent_source.frappe.db.get_value",
			return_value="mazeed-development",
		):
			branch = agent_source.custom_get_agent_repository_branch(server)

		self.assertEqual(branch, "mazeed-development")

	def test_apply_overrides_is_idempotent(self):
		from press.press.doctype.server.server import BaseServer

		original_url_fn = BaseServer.get_agent_repository_url
		original_branch_fn = BaseServer.get_agent_repository_branch
		try:
			agent_source.apply_overrides()
			patched_url_fn = BaseServer.get_agent_repository_url
			agent_source.apply_overrides()

			self.assertIs(BaseServer.get_agent_repository_url, agent_source.custom_get_agent_repository_url)
			self.assertIs(BaseServer.get_agent_repository_url, patched_url_fn)
			self.assertIs(
				BaseServer.get_agent_repository_branch, agent_source.custom_get_agent_repository_branch
			)
		finally:
			BaseServer.get_agent_repository_url = original_url_fn
			BaseServer.get_agent_repository_branch = original_branch_fn
