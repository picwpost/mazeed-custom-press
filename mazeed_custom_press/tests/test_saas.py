from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.api.saas import new_saas_site
from mazeed_custom_press.overrides.saas_site import CustomSaasSite


class TestSaasOverrides(FrappeTestCase):
	def test_new_saas_site_forwards_config_to_pooled_rename(self):
		mock_site = Mock()
		mock_site.rename_pooled_site.return_value = mock_site

		with (
			patch("mazeed_custom_press.api.saas.frappe.only_for"),
			patch("mazeed_custom_press.api.saas.frappe.db.commit"),
			patch("mazeed_custom_press.api.saas.get_pooled_saas_site", return_value="standby-site"),
			patch("mazeed_custom_press.api.saas.CustomSaasSite", return_value=mock_site) as mock_site_cls,
		):
			result = new_saas_site("new-site", "my-app", config={"maintenance_mode": 1})

		mock_site_cls.assert_called_once_with(site="standby-site", app="my-app")
		mock_site.rename_pooled_site.assert_called_once_with(
			subdomain="new-site",
			config={"maintenance_mode": 1},
		)
		self.assertEqual(result, mock_site)

	def test_rename_forwards_pending_config_to_agent(self):
		site = CustomSaasSite.__new__(CustomSaasSite)
		site.server = "server-1"
		site.subdomain = "old-site"
		site.status = "Active"
		site._pending_rename_config = {"maintenance_mode": 1}
		site._get_site_name = Mock(return_value="old-site.example.com")
		site.check_duplicate_site = Mock()
		site.rename_upstream = Mock()
		site.save = Mock()
		site.remove_dns_record = Mock()

		with (
			patch("mazeed_custom_press.overrides.saas_site.create_dns_record"),
			patch("mazeed_custom_press.overrides.saas_site.frappe.get_value", return_value="proxy-1"),
			patch("mazeed_custom_press.overrides.saas_site.Agent") as mock_agent_cls,
			patch("mazeed_custom_press.overrides.saas_site.log_error"),
		):
			mock_agent = Mock()
			mock_agent_cls.return_value = mock_agent

			site.rename("new-site.example.com")

		mock_agent.rename_site.assert_called_once_with(
			site,
			"new-site.example.com",
			config={"maintenance_mode": 1},
		)
		site.rename_upstream.assert_called_once_with("new-site.example.com")
		site.save.assert_called_once()
		site.remove_dns_record.assert_called_once_with("proxy-1")
		self.assertEqual(site.status, "Pending")

	def test_rename_pooled_site_erpnext_commits_before_subscription_and_agent_job(self):
		site = CustomSaasSite.__new__(CustomSaasSite)
		site.app = "my-app"
		site.server = "server-1"
		site.configuration = []
		site._update_configuration = Mock()
		site.save = Mock()
		site.create_subscription = Mock()
		site.reload = Mock()

		call_order = []
		site.save.side_effect = lambda *a, **k: call_order.append("save")
		site.create_subscription.side_effect = lambda *a, **k: call_order.append("create_subscription")

		with (
			patch("mazeed_custom_press.overrides.saas_site.get_saas_site_plan", return_value="free"),
			patch(
				"mazeed_custom_press.overrides.saas_site.frappe.utils.add_days",
				return_value=Mock(strftime=Mock(return_value="2026-08-01")),
			),
			patch(
				"mazeed_custom_press.overrides.saas_site.frappe.db.commit",
				side_effect=lambda: call_order.append("commit"),
			) as mock_commit,
			patch("mazeed_custom_press.overrides.saas_site.Agent") as mock_agent_cls,
		):
			mock_agent = Mock()
			mock_agent_cls.return_value = mock_agent

			result = site._rename_pooled_site_erpnext()

		# The commit must land strictly between the claim/save and the
		# subscription + Agent Job work, otherwise all three still ride in
		# one long transaction and we're back to the original lock timeout.
		self.assertEqual(call_order, ["save", "commit", "create_subscription"])
		mock_commit.assert_called_once()
		mock_agent.update_site_config.assert_called_once_with(site)
		self.assertEqual(result, site)
