import frappe
from frappe.model.document import Document
from frappe.utils import cint


class ReleaseRollout(Document):
	def validate(self):
		if cint(self.max_concurrent_updates) <= 0:
			frappe.throw("Max concurrent updates must be greater than zero")
		if cint(self.canary_size) < 0:
			frappe.throw("Canary size cannot be negative")
		if cint(self.total_sites) and cint(self.canary_size) > cint(self.total_sites):
			frappe.throw("Canary size cannot exceed the total number of sites")
