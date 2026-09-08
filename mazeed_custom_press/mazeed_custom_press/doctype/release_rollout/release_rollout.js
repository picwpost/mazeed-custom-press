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
	frm.fields_dict.dashboard_html.$wrapper.html(render_dashboard_header(summary));
	frm.fields_dict.sites_table_html.$wrapper.html(render_sites_table(summary, sites, view));
	bind_dashboard_controls(frm);
	if (!["Running", "Paused"].includes(summary.status)) clearInterval(frm.rollout_refresh_timer);
}

function bind_dashboard_controls(frm) {
	const view = frm.rollout_view;
	const wrapper = frm.fields_dict.sites_table_html.$wrapper;
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

// Rollout row status -> frappe indicator colour. Terminal-good is green,
// terminal-bad red, in-flight blue, not-yet-started grey.
const ROW_STATUS_COLORS = {
	Pending: "gray",
	Starting: "blue",
	Running: "blue",
	Success: "green",
	Recovered: "green",
	Fatal: "red",
	Skipped: "orange",
	Cancelled: "gray",
};

// The site's own live status, which is what the operator watches during a
// deploy. Deliberately separate from the rollout row status: a row can read
// Running while the site is still Pending, and that gap is the queue wait.
const SITE_STATUS_COLORS = {
	Active: "green",
	Updating: "blue",
	Pending: "orange",
	Recovering: "orange",
	Inactive: "gray",
	Suspended: "gray",
	Broken: "red",
	Archived: "gray",
};

const QUEUE_SEVERITY_COLORS = { ok: "green", warn: "orange", bad: "red" };

function pill(label, color) {
	if (!label) return "";
	const esc = frappe.utils.escape_html;
	return `<span class="indicator-pill ${color || "gray"}">${esc(label)}</span>`;
}

function format_seconds(seconds) {
	if (seconds === null || seconds === undefined) return "";
	const total = Math.max(0, Math.round(seconds));
	const hours = Math.floor(total / 3600);
	const minutes = Math.floor((total % 3600) / 60);
	const secs = total % 60;
	if (hours) return `${hours}h ${minutes}m`;
	if (minutes) return `${minutes}m ${secs}s`;
	return `${secs}s`;
}

function format_duration(start, end, server_time) {
	if (!start) return "";
	const finish = end || server_time;
	if (!finish) return "";
	const seconds = (frappe.datetime.str_to_obj(finish) - frappe.datetime.str_to_obj(start)) / 1000;
	if (!(seconds >= 0)) return "";
	return format_seconds(seconds);
}

function render_dashboard_header(summary) {
	const esc = frappe.utils.escape_html;
	const canary_color = CANARY_COLORS[summary.canary_status] || CANARY_COLORS.Pending;
	const elapsed = format_duration(summary.started_at, summary.finished_at, summary.server_time);
	const progress = Math.min(100, Math.max(0, Number(summary.progress_percent || 0)));

	// A stacked bar rather than one green fill: at a glance it shows how the
	// rollout is composed, not just how far along it is. A run that is 90%
	// done and a run that is 90% done with a fifth of it failed should not
	// look identical.
	const total = Number(summary.total_sites || 0);
	const composition = [
		[summary.success_sites, "var(--green-500)", __("Success")],
		[summary.recovered_sites, "var(--cyan-500)", __("Recovered")],
		[summary.failed_sites, "var(--red-500)", __("Fatal")],
		[summary.skipped_sites, "var(--orange-500)", __("Skipped")],
		[summary.cancelled_sites, "var(--gray-500)", __("Cancelled")],
		[Number(summary.starting_sites || 0) + Number(summary.running_sites || 0), "var(--blue-400)", __("In flight")],
	];
	const segments = total
		? composition
				.filter(([count]) => Number(count || 0) > 0)
				.map(
					([count, color, label]) =>
						`<div title="${esc(label)}: ${Number(count)}" style="width:${((Number(count) / total) * 100).toFixed(2)}%;background:${color};transition:width .3s"></div>`
				)
				.join("")
		: "";

	const header = `
		<div class="mb-3" style="display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center">
			${pill(summary.status, ROW_STATUS_COLORS[summary.status] || "blue")}
			<span>${__("Stage")}: <strong>${esc(summary.stage)}</strong></span>
			<span style="display:inline-flex;align-items:center;gap:4px">
				<span style="width:10px;height:10px;border-radius:50%;background:${canary_color};display:inline-block"></span>
				${__("Canary")}: <strong>${esc(summary.canary_status)}</strong>
			</span>
			<span>${__("Release Group")}: ${esc(summary.release_group || "")}</span>
			<span>${__("Limit")}: ${summary.max_concurrent_updates}</span>
			<span>${__("Active")}: ${summary.active_count}</span>
		</div>
		<div class="mb-3" style="display:flex;align-items:center;gap:12px">
			<div style="flex:1;height:10px;border-radius:5px;background:var(--gray-200);overflow:hidden;display:flex">
				${segments}
			</div>
			<strong style="font-variant-numeric:tabular-nums;min-width:52px;text-align:right">${progress.toFixed(1)}%</strong>
		</div>
		<div class="mb-3 text-muted" style="display:flex;flex-wrap:wrap;gap:16px">
			<span>${__("Started by")}: ${esc(summary.started_by || "")}</span>
			<span>${__("Started")}: ${esc(summary.started_at || "")}</span>
			<span>${__("Elapsed")}: ${esc(elapsed)}</span>
			<span>${__("Finished")}: ${esc(summary.finished_at || "")}</span>
		</div>`;

	// The queue-wait panel is the diagnostic: it separates time spent waiting
	// for an agent worker from time spent actually updating, which is the
	// difference between "the deploy is slow" and "the update is slow".
	const median = summary.queue_wait_median;
	const severity =
		median === null || median === undefined
			? "gray"
			: median >= 300
			  ? "red"
			  : median >= 60
			    ? "orange"
			    : "green";
	const queue_panel = `
		<div class="border rounded p-3 mb-3" style="border-left:3px solid var(--${severity}-500,grey)">
			<div style="display:flex;flex-wrap:wrap;gap:24px;align-items:baseline">
				<div>
					<div class="text-muted">${__("Median queue wait")}</div>
					<strong style="font-size:var(--text-xl);font-variant-numeric:tabular-nums">
						${median === null || median === undefined ? "&mdash;" : esc(format_seconds(median))}
					</strong>
				</div>
				<div>
					<div class="text-muted">${__("Worst queue wait")}</div>
					<strong style="font-variant-numeric:tabular-nums">
						${summary.queue_wait_max === null || summary.queue_wait_max === undefined ? "&mdash;" : esc(format_seconds(summary.queue_wait_max))}
					</strong>
				</div>
				<div class="text-muted" style="flex:1;min-width:220px">
					${__("Time from Site Update created to an agent worker starting it — the site sits Pending for exactly this long. Measured over {0} site(s).", [summary.queue_wait_samples || 0])}
				</div>
			</div>
		</div>`;

	// Grouped by meaning rather than listed flat: what is happening now, what
	// finished, and what needs a human. A flat row of ten equal tiles gives a
	// zero-value "Fatal" the same weight as a Fatal of twelve, which is the
	// opposite of useful during a deploy.
	const groups = [
		{
			title: __("In flight"),
			hint: __("Limit {0}", [summary.max_concurrent_updates]),
			tiles: [
				{ label: __("Starting"), value: summary.starting_sites, hue: "blue" },
				{ label: __("Updating"), value: summary.running_sites, hue: "blue" },
				{ label: __("Waiting"), value: summary.pending_sites, hue: "gray" },
			],
		},
		{
			title: __("Completed"),
			hint: __("{0} of {1}", [summary.completed_count || 0, summary.total_sites || 0]),
			tiles: [
				{ label: __("Success"), value: summary.success_sites, hue: "green" },
				{ label: __("Recovered"), value: summary.recovered_sites, hue: "cyan" },
			],
		},
		{
			title: __("Needs attention"),
			tiles: [
				{ label: __("Fatal"), value: summary.failed_sites, hue: "red" },
				{ label: __("Skipped"), value: summary.skipped_sites, hue: "orange" },
				{ label: __("Cancelled"), value: summary.cancelled_sites, hue: "gray" },
			],
		},
	];

	// A tile only takes its colour once it has something in it. Zeroes stay
	// quiet so the eye lands on the counts that actually exist.
	const tile = ({ label, value, hue }) => {
		const count = Number(value || 0);
		const live = count > 0;
		const border = live ? `var(--${hue}-500)` : "var(--border-color)";
		const background = live ? `var(--${hue}-50)` : "var(--card-bg)";
		const number_color = live ? `var(--${hue}-700)` : "var(--text-light)";
		return `
			<div style="flex:1 1 96px;min-width:96px;padding:10px 12px;border:1px solid var(--border-color);
				border-top:3px solid ${border};border-radius:var(--border-radius-md,6px);background:${background}">
				<div style="font-size:var(--text-xs);color:var(--text-muted);text-transform:uppercase;
					letter-spacing:0.04em;white-space:nowrap">${esc(label)}</div>
				<div style="font-size:var(--text-2xl,22px);font-weight:600;line-height:1.15;
					font-variant-numeric:tabular-nums;color:${number_color}">${count}</div>
			</div>`;
	};

	const group_html = groups
		.map(
			(group) => `
		<div style="flex:1 1 260px;min-width:240px">
			<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
				<span style="font-size:var(--text-sm);font-weight:600">${esc(group.title)}</span>
				${group.hint ? `<span style="font-size:var(--text-xs);color:var(--text-muted)">${esc(group.hint)}</span>` : ""}
			</div>
			<div style="display:flex;gap:8px">${group.tiles.map(tile).join("")}</div>
		</div>`
		)
		.join("");

	return `
		${header}
		${queue_panel}
		<div style="display:flex;flex-wrap:wrap;gap:20px" class="mb-3">${group_html}</div>`;
}

function render_sites_table(summary, sites, view) {
	const esc = frappe.utils.escape_html;
	const statuses = ["", "Pending", "Starting", "Running", "Success", "Recovered", "Fatal", "Skipped", "Cancelled"];
	const status_options = statuses
		.map(
			(status) =>
				`<option value="${status}" ${view.status === status ? "selected" : ""}>${status || __("All Statuses")}</option>`
		)
		.join("");
	const stages = [["", __("Canary + Main")], ["Canary", __("Canary only")], ["Main", __("Main only")]];
	const stage_options = stages
		.map(([value, label]) => `<option value="${value}" ${view.stage === value ? "selected" : ""}>${label}</option>`)
		.join("");
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

	const rows = sites
		.map((row) => {
			const queue_color = QUEUE_SEVERITY_COLORS[row.queue_severity] || "gray";
			const queue_cell =
				row.queue_seconds === null || row.queue_seconds === undefined
					? ""
					: `<span class="indicator-pill ${queue_color}" style="font-variant-numeric:tabular-nums">${esc(format_seconds(row.queue_seconds))}</span>`;
			const retry =
				row.retry_count > 0
					? ` <span class="indicator-pill red" title="${__("Delivery retries — the job could not be handed to the agent first time")}">${__("retry")} ${row.retry_count}</span>`
					: "";
			return `
		<tr>
			<td><a href="/app/site/${encodeURIComponent(row.site)}">${esc(row.site)}</a></td>
			<td>${row.is_canary ? pill(__("Canary"), "purple") : ""}</td>
			<td>${pill(row.status, ROW_STATUS_COLORS[row.status])}</td>
			<td>${pill(row.site_status, SITE_STATUS_COLORS[row.site_status])}</td>
			<td>${queue_cell}${retry}</td>
			<td style="font-variant-numeric:tabular-nums">${esc(format_seconds(row.update_seconds))}</td>
			<td>${row.deploy_type ? pill(row.deploy_type, row.deploy_type === "Migrate" ? "orange" : "blue") : ""}</td>
			<td>${esc(row.source_bench || "")}</td>
			<td>${row.site_update ? `<a href="/app/site-update/${encodeURIComponent(row.site_update)}">${__("open")}</a>` : ""}</td>
			<td class="text-danger">${esc(row.last_error || "")}</td>
		</tr>`;
		})
		.join("");

	return `
		${controls}
		<div class="table-responsive"><table class="table table-bordered table-sm">
		<thead><tr>
			<th>${__("Site")}</th>
			<th>${__("Stage")}</th>
			<th>${__("Rollout")}</th>
			<th>${__("Site Now")}</th>
			<th title="${__("Waiting for an agent worker. The site reads Pending for this long.")}">${__("Queued")}</th>
			<th title="${__("Actual update work once a worker picked it up.")}">${__("Updating")}</th>
			<th>${__("Type")}</th>
			<th>${__("Bench")}</th>
			<th>${__("Update")}</th>
			<th>${__("Last Error")}</th>
		</tr></thead>
		<tbody>${rows || `<tr><td colspan="10">${__("No sites")}</td></tr>`}</tbody></table></div>`;
}
