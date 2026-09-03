"""Cap how long an undelivered Agent Job waits before its next attempt.

Press computes the delay as `retry_count ** 5` seconds, which reaches 1024s on
the 4th attempt and 7776s on the 6th -- the default `max_retry_count` in the
Agent Job Type fixtures -- so a job can sit undelivered for over two hours.

It takes only one slow agent response to get there. Any Agent Request Failure
row opens `Agent.should_skip_requests()` for the whole server, every queued job
for it burns a retry, and the backoff is never reset when the server recovers.
`remove_old_failures()` pings each failing agent every minute and deletes the
row the moment it answers, so that circuit breaker -- not this backoff -- is
what protects a server that is genuinely down. Capping the tail only shortens
how long a healthy server's jobs stay parked after it comes back; the first
three delays are left exactly as Press has them.
"""

from __future__ import annotations

MAX_RETRY_DELAY_IN_SECONDS = 300

_original_get_next_retry_at = None


def custom_get_next_retry_at(job_retry_count):
	from frappe.utils import add_to_date, now_datetime

	backoff_in_seconds = 5
	retry_in_seconds = min(job_retry_count**backoff_in_seconds, MAX_RETRY_DELAY_IN_SECONDS)

	return add_to_date(now_datetime(), seconds=retry_in_seconds)


def apply_overrides():
	"""Patch Press's module-level get_next_retry_at for this worker/process.

	`AgentJob.set_status_and_next_retry_at` resolves the name off the module at
	call time, exactly like `update_status` does, so replacing the module
	attribute is enough -- there is no class method to override.
	"""
	global _original_get_next_retry_at
	from press.press.doctype.agent_job import agent_job as agent_job_module

	if agent_job_module.get_next_retry_at is not custom_get_next_retry_at:
		if _original_get_next_retry_at is None:
			_original_get_next_retry_at = agent_job_module.get_next_retry_at
		agent_job_module.get_next_retry_at = custom_get_next_retry_at
