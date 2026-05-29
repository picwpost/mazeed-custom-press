from __future__ import annotations

import re

import frappe
from frappe.website.page_renderers.base_renderer import BaseRenderer
from frappe.utils.response import build_response

from mazeed_custom_press.api.release_group_script import (
	create_release_group_script_job,
	get_release_group_script_job_detail,
)


class ReleaseGroupScriptPage(BaseRenderer):
	POST_PATH = "server/run-release-group-script"
	JOB_PATH_RE = re.compile(r"^jobs/(?P<job_id>\d+)$")

	def can_render(self):
		method = getattr(frappe.local.request, "method", "").upper()
		if method == "POST" and self.path == self.POST_PATH:
			return True
		if method == "GET" and self.JOB_PATH_RE.fullmatch(self.path):
			return True
		return False

	def render(self):
		try:
			method = getattr(frappe.local.request, "method", "").upper()
			frappe.local.response.clear()
			if method == "POST":
				frappe.local.response.update(create_release_group_script_job())
				return build_response("json")

			match = self.JOB_PATH_RE.fullmatch(self.path)
			if not match:
				return self._json_error("Not Permitted", 403)

			detail = get_release_group_script_job_detail(match.group("job_id"))
			frappe.local.response.update(detail)
			return build_response("json")
		except frappe.PermissionError:
			return self._json_error("Not Permitted", 403)
		except frappe.DoesNotExistError:
			return self._json_error("Not Found", 404)
		except Exception as exc:
			return self._json_error(str(exc), 400)

	def _json_error(self, message: str, status_code: int):
		frappe.local.response.clear()
		frappe.local.response["error"] = message
		frappe.local.response["http_status_code"] = status_code
		return build_response("json")
