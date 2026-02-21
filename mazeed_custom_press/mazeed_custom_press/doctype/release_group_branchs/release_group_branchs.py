import frappe
from frappe.model.document import Document


class ReleaseGroupBranchs(Document):
	def validate(self):
		# Normalize inputs first so empty strings don't bypass validations.
		self._normalize_branch_values()
		# At least one of the two branch fields must be provided.
		self._validate_at_least_one_branch()
		# Prevent duplicate rows for the same release group + branch combination.
		self._validate_unique_combination()

	def _normalize_branch_values(self):
		self.mazeed_theme_branch = (self.mazeed_theme_branch or "").strip() or None
		self.feature_flag_branch = (self.feature_flag_branch or "").strip() or None

	def _validate_unique_combination(self):
		# Exclude current doc so updates don't match themselves.
		filters = {
			"release_group": self.release_group,
			"mazeed_theme_branch": self.mazeed_theme_branch,
			"feature_flag_branch": self.feature_flag_branch,
			"name": ("!=", self.name),
		}
		if frappe.db.exists("Release Group Branchs", filters):
			frappe.throw(
				"A record already exists for this Release Group with the same Mazeed Theme Branch and Feature Flag Branch."
			)

	def _validate_at_least_one_branch(self):
		if not self.mazeed_theme_branch and not self.feature_flag_branch:
			frappe.throw("At least one branch is required: Mazeed Theme Branch or Feature Flag Branch.")
