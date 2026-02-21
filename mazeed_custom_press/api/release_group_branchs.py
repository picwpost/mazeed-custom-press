import frappe


DOCTYPE = "Release Group Branchs"

# ---------------------------------------------------------------------
# Create new release group branch 
# ---------------------------------------------------------------------
@frappe.whitelist()
def new(
	release_group, user, mazeed_theme_branch=None, feature_flag_branch=None
):
	frappe.only_for("System Manager")

	# Create one row; controller validations handle uniqueness and branch requirements.
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"release_group": release_group,
			"user": user,
			"mazeed_theme_branch": mazeed_theme_branch,
			"feature_flag_branch": feature_flag_branch,
		}
	).insert(ignore_permissions=True)

	frappe.db.commit()
	return doc.as_dict()


# ---------------------------------------------------------------------
# List or get release group branches with optional filters for release 
# group, user, and branch names.
# ---------------------------------------------------------------------
@frappe.whitelist()
def get(
	name=None,
	release_group=None,
	user=None,
	mazeed_theme_branch=None,
	feature_flag_branch=None,
	limit=20,
	start=0,
):
	frappe.only_for("System Manager")

	# If name is passed, return a single document directly.
	if name:
		return frappe.get_doc(DOCTYPE, name).as_dict()

	# Optional filters are combined with AND in frappe.get_all.
	filters = {}
	if release_group:
		filters["release_group"] = release_group
	if user:
		filters["user"] = user
	if mazeed_theme_branch:
		filters["mazeed_theme_branch"] = mazeed_theme_branch
	if feature_flag_branch:
		filters["feature_flag_branch"] = feature_flag_branch

	return frappe.get_all(
		DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"release_group",
			"user",
			"mazeed_theme_branch",
			"feature_flag_branch",
			"creation",
			"modified",
		],
		order_by="modified desc",
		limit_start=int(start),
		limit_page_length=int(limit),
	)
