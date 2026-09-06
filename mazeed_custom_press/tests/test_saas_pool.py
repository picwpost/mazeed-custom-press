from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.overrides.saas_pool import custom_get


class _FakeCursor:
	"""Stand-in for frappe.db._cursor exposing just .rowcount."""

	def __init__(self, rowcount=0):
		self.rowcount = rowcount


def _pool(app="my-app"):
	pool = Mock()
	pool.app = app
	return pool


class TestCustomGet(FrappeTestCase):
	def test_claims_first_candidate_when_update_affects_a_row(self):
		cursor = _FakeCursor(rowcount=1)

		with (
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.get_all",
				return_value=["workspace-1", "workspace-2"],
			),
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db.sql") as mock_sql,
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db._cursor", cursor),
		):
			result = custom_get(_pool(), "")

		self.assertEqual(result, "workspace-1")
		mock_sql.assert_called_once_with(
			"UPDATE `tabSite` SET is_standby = 0 WHERE name = %s AND is_standby = 1",
			"workspace-1",
		)

	def test_falls_through_to_next_candidate_when_claim_is_lost(self):
		cursor = _FakeCursor()
		rowcounts = iter([0, 1])

		def fake_sql(*args, **kwargs):
			cursor.rowcount = next(rowcounts)

		with (
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.get_all",
				return_value=["workspace-1", "workspace-2"],
			),
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.db.sql",
				side_effect=fake_sql,
			) as mock_sql,
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db._cursor", cursor),
		):
			result = custom_get(_pool(), "")

		self.assertEqual(result, "workspace-2")
		self.assertEqual(mock_sql.call_count, 2)

	def test_returns_none_when_no_candidate_can_be_claimed(self):
		cursor = _FakeCursor(rowcount=0)

		with (
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.get_all",
				return_value=["workspace-1", "workspace-2"],
			),
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db.sql"),
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db._cursor", cursor),
		):
			result = custom_get(_pool(), "")

		self.assertIsNone(result)

	def test_returns_none_without_querying_update_when_no_candidates_exist(self):
		with (
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.get_all",
				return_value=[],
			),
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db.sql") as mock_sql,
		):
			result = custom_get(_pool(), "")

		self.assertIsNone(result)
		mock_sql.assert_not_called()

	def test_filters_scope_to_named_hybrid_pool_when_provided(self):
		with (
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.get_all",
				return_value=[],
			) as mock_get_all,
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db.sql"),
		):
			custom_get(_pool(), "pool-a")

		filters = mock_get_all.call_args[0][1]
		self.assertEqual(filters["hybrid_saas_pool"], "pool-a")

	def test_filters_exclude_hybrid_pools_when_none_provided(self):
		with (
			patch(
				"mazeed_custom_press.overrides.saas_pool.frappe.get_all",
				return_value=[],
			) as mock_get_all,
			patch("mazeed_custom_press.overrides.saas_pool.frappe.db.sql"),
		):
			custom_get(_pool(), "")

		filters = mock_get_all.call_args[0][1]
		self.assertEqual(filters["hybrid_saas_pool"], ("is", "not set"))
