frappe.ui.form.on("Release Rollout", {
	refresh(frm) {
		frm.disable_save();
		frm.rollout_view = frm.rollout_view || { start: 0, page_length: 50, status: "", stage: "" };
		frm.add_custom_button(__("Refresh Dashboard"), () => refresh_rollout_dashboard(frm));
		add_operator_buttons(frm);
		refresh_rollout_dashboard(frm);
		clearInterval(frm.rollout_refresh_timer);
		if (["Running", "Paused"].includes(frm.doc.status)) {
			frm.rollout_refresh_timer = setInterval(() => refresh_rollout_dashboard(frm), 8000);
		}
	},

	onload_post_render(frm) {
		$(frm.wrapper).on("remove", () => clearInterval(frm.rollout_refresh_timer));
	},
});

function add_operator_buttons(frm) {
	const call_action = async (method) => {
		await frappe.call(`mazeed_custom_press.api.release_rollout.${method}`, { name: frm.doc.name });
		frm.reload_doc();
	};
	if (frm.doc.status === "Running") {
		frm.add_custom_button(__("Pause"), () => call_action("pause_rollout"));
	}
	if (frm.doc.status === "Paused") {
		frm.add_custom_button(__("Resume"), () => call_action("resume_rollout"));
	}
	if (["Running", "Paused"].includes(frm.doc.status)) {
		frm.add_custom_button(__("Cancel Rollout"), () =>
			frappe.confirm(
				__(
					"Stop this rollout? Sites that have not started will be cancelled. Updates already in flight will finish, but no new sites will start."
				),
				() => call_action("cancel_rollout")
			)
		);
	}
}

async function refresh_rollout_dashboard(frm) {
	if (frm.is_new()) return;
	const view = frm.rollout_view;
	const [summary_response, sites_response] = await Promise.all([
		frappe.call("mazeed_custom_press.api.release_rollout.get_rollout_summary", { name: frm.doc.name }),
		frappe.call("mazeed_custom_press.api.release_rollout.get_rollout_sites", {
			name: frm.doc.name,
			status: view.status || null,
			stage: view.stage || null,
			start: view.start,
			page_length: view.page_length,
		}),
	]);
	const summary = summary_response.message;
	const sites = sites_response.message || [];
	frm.fields_dict.dashboard_html.$wrapper.html(render_rollout_dashboard(summary, sites, view));
	bind_dashboard_controls(frm);
	if (!["Running", "Paused"].includes(summary.status)) clearInterval(frm.rollout_refresh_timer);
}

function bind_dashboard_controls(frm) {
	const view = frm.rollout_view;
	const wrapper = frm.fields_dict.dashboard_html.$wrapper;
	wrapper.find(".rollout-status-filter").on("change", function () {
		view.status = this.value;
		view.start = 0;
		refresh_rollout_dashboard(frm);
	});
	wrapper.find(".rollout-stage-filter").on("change", function () {
		view.stage = this.value;
		view.start = 0;
		refresh_rollout_dashboard(frm);
	});
	wrapper.find(".rollout-prev-page").on("click", () => {
		view.start = Math.max(0, view.start - view.page_length);
		refresh_rollout_dashboard(frm);
	});
	wrapper.find(".rollout-next-page").on("click", () => {
		view.start += view.page_length;
		refresh_rollout_dashboard(frm);
	});
}

const CANARY_COLORS = {
	Pending: "var(--gray-500, grey)",
	Running: "var(--blue-500, blue)",
	Passed: "var(--green-500, green)",
	Failed: "var(--red-500, red)",
};

function format_duration(start, end, server_time) {
	if (!start) return "";
	const finish = end || server_time;
	if (!finish) return "";
	const seconds = (frappe.datetime.str_to_obj(finish) - frappe.datetime.str_to_obj(start)) / 1000;
	if (!(seconds >= 0)) return "";
	const hours = Math.floor(seconds / 3600);
	const minutes = Math.floor((seconds % 3600) / 60);
	const secs = Math.floor(seconds % 60);
	return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${secs}s` : `${secs}s`;
}

function render_rollout_dashboard(summary, sites, view) {
	const esc = frappe.utils.escape_html;
	const canary_color = CANARY_COLORS[summary.canary_status] || CANARY_COLORS.Pending;
	const elapsed = format_duration(summary.started_at, summary.finished_at, summary.server_time);

	const header = `
		<div class="mb-3" style="display:flex;flex-wrap:wrap;gap:16px;align-items:center">
			<strong style="font-size:var(--text-lg)">${esc(summary.status)}</strong>
			<span>${__("Stage")}: <strong>${esc(summary.stage)}</strong></span>
			<span style="display:inline-flex;align-items:center;gap:4px">
				<span style="width:10px;height:10px;border-radius:50%;background:${canary_color};display:inline-block"></span>
				${__("Canary")}: <strong>${esc(summary.canary_status)}</strong>
			</span>
			<span>${__("Release Group")}: ${esc(summary.release_group || "")}</span>
			<span>${__("Limit")}: ${summary.max_concurrent_updates}</span>
			<span>${__("Active")}: ${summary.active_count}</span>
			<span>${__("Progress")}: ${Number(summary.progress_percent || 0).toFixed(1)}%</span>
		</div>
		<div class="mb-3 text-muted" style="display:flex;flex-wrap:wrap;gap:16px">
			<span>${__("Started by")}: ${esc(summary.started_by || "")}</span>
			<span>${__("Started")}: ${esc(summary.started_at || "")}</span>
			<span>${__("Elapsed")}: ${esc(elapsed)}</span>
			<span>${__("Finished")}: ${esc(summary.finished_at || "")}</span>
		</div>`;

	const cards = [
		[__("Total"), summary.total_sites],
		[__("Pending"), summary.pending_sites],
		[__("Starting"), summary.starting_sites],
		[__("Updating"), summary.running_sites],
		[__("Success"), summary.success_sites],
		[__("Recovered"), summary.recovered_sites],
		[__("Fatal"), summary.failed_sites],
		[__("Skipped"), summary.skipped_sites],
		[__("Cancelled"), summary.cancelled_sites],
		[__("Updated"), summary.updated_sites],
		[__("Completed"), summary.completed_count],
	];
	const card_html = cards.map(([label, value]) => `
		<div class="border rounded p-3"><div class="text-muted">${esc(label)}</div><strong>${value || 0}</strong></div>
	`).join("");

	const statuses = ["", "Pending", "Starting", "Running", "Success", "Recovered", "Fatal", "Skipped", "Cancelled"];
	const status_options = statuses.map((status) =>
		`<option value="${status}" ${view.status === status ? "selected" : ""}>${status || __("All Statuses")}</option>`
	).join("");
	const stages = [["", __("Canary + Main")], ["Canary", __("Canary only")], ["Main", __("Main only")]];
	const stage_options = stages.map(([value, label]) =>
		`<option value="${value}" ${view.stage === value ? "selected" : ""}>${label}</option>`
	).join("");
	const page = Math.floor(view.start / view.page_length) + 1;
	const controls = `
		<div class="mb-2" style="display:flex;gap:8px;align-items:center">
			<select class="form-control rollout-status-filter" style="width:auto">${status_options}</select>
			<select class="form-control rollout-stage-filter" style="width:auto">${stage_options}</select>
			<span style="margin-left:auto"></span>
			<button class="btn btn-xs btn-default rollout-prev-page" ${view.start === 0 ? "disabled" : ""}>${__("Prev")}</button>
			<span class="text-muted">${__("Page")} ${page}</span>
			<button class="btn btn-xs btn-default rollout-next-page" ${sites.length < view.page_length ? "disabled" : ""}>${__("Next")}</button>
		</div>`;

	const rows = sites.map((row) => `
		<tr>
			<td><a href="/app/site/${encodeURIComponent(row.site)}">${esc(row.site)}</a></td>
			<td>${row.is_canary ? __("Yes") : __("No")}</td>
			<td>${esc(row.source_bench || "")}</td><td>${esc(row.status)}</td>
			<td>${row.site_update ? `<a href="/app/site-update/${encodeURIComponent(row.site_update)}">${esc(row.site_update)}</a>` : ""}</td>
			<td>${esc(row.started_at || "")}</td><td>${esc(row.finished_at || "")}</td>
			<td>${esc(format_duration(row.started_at, row.finished_at, summary.server_time))}</td>
			<td>${esc(row.last_error || "")}</td>
		</tr>
	`).join("");
	return `
		${header}
		<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px" class="mb-4">${card_html}</div>
		${controls}
		<div class="table-responsive"><table class="table table-bordered table-sm">
		<thead><tr><th>${__("Site")}</th><th>${__("Canary")}</th><th>${__("Bench")}</th><th>${__("Status")}</th>
		<th>${__("Site Update")}</th><th>${__("Started")}</th><th>${__("Finished")}</th><th>${__("Duration")}</th><th>${__("Last Error")}</th></tr></thead>
		<tbody>${rows || `<tr><td colspan="9">${__("No sites")}</td></tr>`}</tbody></table></div>`;
}
