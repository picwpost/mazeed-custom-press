frappe.ui.form.on("Release Group", {
	refresh(frm) {
		frm.add_custom_button("Run Script", () => {
			const d = new frappe.ui.Dialog({
				title: "Run Script on Release Group",
				fields: [
					{
						fieldtype: "Code",
						fieldname: "script",
						label: "Bash Script",
						options: "Shell",
						reqd: 1,
					},
					{
						fieldtype: "Int",
						fieldname: "timeout",
						label: "Timeout (seconds)",
						default: 300,
					},
				],
				primary_action_label: "Run",
				primary_action(values) {
					frappe.call({
						method: "mazeed_custom_press.api.release_group_script.run_release_group_script",
						args: {
							release_group: frm.doc.name,
							script: values.script,
							timeout: values.timeout || 300,
						},
						callback(r) {
							d.hide();
							if (r.message && r.message.job) {
								frappe.msgprint({
									title: "Script Job Started",
									message: `Job <a href="/app/release-group-script-run/${r.message.job}">${r.message.job}</a> created.`,
									indicator: "green",
								});
							}
						},
					});
				},
			});
			d.show();
		});
	},
});
