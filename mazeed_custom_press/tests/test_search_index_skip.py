"""Skipping the website search index rebuild.

The flag reaches the agent by amending the queued Agent Job's request body,
which is only valid because the job is created but not yet delivered. These
tests pin that, and that the default changes nothing.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from mazeed_custom_press.overrides import search_index


class SearchIndexSkipCase(FrappeTestCase):
    def setUp(self):
        search_index.apply_overrides()

    def tearDown(self):
        frappe.db.rollback()

    def set_flag(self, value: int):
        frappe.db.set_single_value("Press Settings", search_index.SETTING, value)


class TestShouldSkip(SearchIndexSkipCase):
    def test_defaults_to_off_so_nothing_changes_until_it_is_ticked(self):
        self.set_flag(0)
        self.assertFalse(search_index.should_skip())

    def test_reports_on_once_enabled(self):
        self.set_flag(1)
        self.assertTrue(search_index.should_skip())

    def test_treated_as_off_when_the_field_does_not_exist_yet(self):
        # Code can reach production before its migrate; a missing field must
        # not raise on every site update.
        self.set_flag(1)
        meta = frappe.get_meta("Press Settings")
        with patch.object(meta, "has_field", return_value=False), patch(
            "frappe.get_meta", return_value=meta
        ):
            self.assertFalse(search_index.should_skip())


class TestRequestBodyIsAmended(SearchIndexSkipCase):
    def make_job(self, request_data: str | None):
        """A stand-in for the freshly created, not-yet-delivered Agent Job."""
        return frappe.get_doc(
            {
                "doctype": "Agent Job",
                "status": "Undelivered",
                "job_type": "Update Site Migrate",
                "server_type": "Server",
                "request_method": "POST",
                "request_path": "/benches/b/sites/s/update/migrate",
                "request_data": request_data,
            }
        ).insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)

    def test_the_flag_is_added_to_the_request_body_when_enabled(self):
        self.set_flag(1)
        job = self.make_job(frappe.as_json({"target": "bench-2", "skip_backups": False}))

        search_index._add_flag_to_request(job)

        sent = frappe.parse_json(frappe.db.get_value("Agent Job", job.name, "request_data"))
        self.assertIs(sent["build_search_index"], False)
        self.assertEqual(sent["target"], "bench-2", "existing keys must survive untouched")

    def test_the_body_is_left_alone_when_the_flag_is_off(self):
        self.set_flag(0)
        original = frappe.as_json({"target": "bench-2"})
        job = self.make_job(original)

        search_index._add_flag_to_request(job)

        sent = frappe.parse_json(frappe.db.get_value("Agent Job", job.name, "request_data"))
        self.assertNotIn("build_search_index", sent)

    def test_a_job_with_no_body_still_gets_the_flag(self):
        # Agent.activate_site sends no data at all today.
        self.set_flag(1)
        job = self.make_job(None)

        search_index._add_flag_to_request(job)

        sent = frappe.parse_json(frappe.db.get_value("Agent Job", job.name, "request_data"))
        self.assertEqual(sent, {"build_search_index": False})

    def test_a_malformed_body_is_logged_and_never_fails_the_update(self):
        # This is an optimisation. Worst case the agent rebuilds the index
        # exactly as it does today.
        self.set_flag(1)
        job = self.make_job("{not valid json")

        search_index._add_flag_to_request(job)  # must not raise

    def test_no_job_is_a_no_op(self):
        self.set_flag(1)
        self.assertIsNone(search_index._add_flag_to_request(None))


class TestOverridesArePatchedOnce(SearchIndexSkipCase):
    def test_patching_twice_does_not_lose_the_original_method(self):
        from press.agent import Agent

        search_index.apply_overrides()
        search_index.apply_overrides()

        self.assertIs(Agent.update_site, search_index.custom_update_site)
        self.assertIs(Agent.activate_site, search_index.custom_activate_site)
        self.assertIsNot(search_index._original_update_site, search_index.custom_update_site)
        self.assertIsNot(search_index._original_activate_site, search_index.custom_activate_site)


class TestEndToEndThroughPressOwnChain(SearchIndexSkipCase):
    """The flag must survive the real path: schedule_update -> SiteUpdate ->
    Agent.update_site -> Agent Job request body.

    Everything above tests the pieces. This is the only test that proves the
    override is actually reached when Press, not the test, drives the call.
    """

    def schedule_and_read_request(self) -> dict:
        from press.press.doctype.agent_job.agent_job import AgentJob

        from mazeed_custom_press.tests.rollout_test_utils import (
            create_updateable_site_environment,
        )

        environment = create_updateable_site_environment()
        with patch.object(AgentJob, "enqueue_http_request", new=lambda self: None):
            environment.site.schedule_update()

        job = frappe.get_last_doc("Agent Job", {"job_type": ("like", "Update Site%")})
        return frappe.parse_json(job.request_data or "{}")

    def test_the_agent_is_told_to_skip_when_the_setting_is_on(self):
        self.set_flag(1)
        self.assertIs(self.schedule_and_read_request().get("build_search_index"), False)

    def test_the_agent_is_told_nothing_when_the_setting_is_off(self):
        self.set_flag(0)
        request = self.schedule_and_read_request()
        self.assertNotIn(
            "build_search_index",
            request,
            "with the setting off the request must be byte-identical to Press's own",
        )
