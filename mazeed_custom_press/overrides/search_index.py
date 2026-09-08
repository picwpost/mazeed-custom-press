"""Stop paying for the website search index on sites that never serve one.

`Build Search Index` runs `bench build-search-index`, which rebuilds the Whoosh
`web_routes` index used only by `frappe.search.web_search` -- the search box on
the public website. It is unrelated to desk global search, the awesome bar and
link lookups.

Building it renders every public route in every installed app, one full
server-side page render at a time, serially. On a workspace with no public
portal that is minutes per site that nothing ever reads.

It runs after `Disable Maintenance Mode`, so it never extended downtime -- but
it keeps the agent job Running, which keeps the Site Update Running, which
keeps a rollout slot occupied. That is the cost worth removing.

Skipping is safe: FullTextSearch.get_index() catches EmptyIndexError and
creates an empty index on demand, so a site that never builds one returns zero
portal-search results rather than erroring. Nothing rebuilds the index on a
schedule either, so it is already stale between deploys today.
"""

from __future__ import annotations

import frappe

SETTING = "skip_build_search_index"

_original_update_site = None
_original_activate_site = None


def should_skip() -> bool:
    """Whether to tell the agent not to rebuild the website search index.

    Defaults to off, so nothing changes until the setting is ticked. Guards the
    field's existence because code can reach production before its migrate.
    """
    if not frappe.get_meta("Press Settings").has_field(SETTING):
        return False
    return bool(frappe.utils.cint(frappe.db.get_single_value("Press Settings", SETTING)))


def custom_update_site(self, *args, **kwargs):
    """Override for press.agent.Agent.update_site.

    Adds `build_search_index` to the request body. Press's own signature is
    left alone and the flag is injected here, so a Press upgrade that changes
    that method's arguments cannot break this.
    """
    job = _original_update_site(self, *args, **kwargs)
    return _add_flag_to_request(job)


def custom_activate_site(self, *args, **kwargs):
    """Override for press.agent.Agent.activate_site."""
    job = _original_activate_site(self, *args, **kwargs)
    return _add_flag_to_request(job)


def _add_flag_to_request(job):
    """Rewrite the queued Agent Job's request body to carry the flag.

    The Agent Job is created but not yet delivered -- `create_agent_job`
    enqueues `create_http_request` with `enqueue_after_commit=True` -- so the
    body can still be amended here, before it is ever sent.
    """
    if not job or not should_skip():
        return job
    try:
        data = frappe.parse_json(job.request_data) if job.request_data else {}
        data["build_search_index"] = False
        frappe.db.set_value(
            "Agent Job", job.name, "request_data", frappe.as_json(data), update_modified=False
        )
        job.request_data = frappe.as_json(data)
    except Exception:
        # Never fail an update over an optimisation. Worst case the agent
        # rebuilds the index exactly as it does today.
        frappe.log_error(title=f"Could not skip search index for Agent Job {job.name}")
    return job


def apply_overrides():
    """Patch press.agent.Agent at runtime for the current worker/process."""
    global _original_update_site, _original_activate_site
    from press.agent import Agent

    if Agent.update_site is not custom_update_site:
        if _original_update_site is None:
            _original_update_site = Agent.update_site
        Agent.update_site = custom_update_site

    if Agent.activate_site is not custom_activate_site:
        if _original_activate_site is None:
            _original_activate_site = Agent.activate_site
        Agent.activate_site = custom_activate_site
