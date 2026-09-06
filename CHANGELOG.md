# Changelog

Running log of changes made to `mazeed_custom_press`. Every commit made anywhere
under `mazeed-press-helper/` must add an entry here (see the rule in the root
`CLAUDE.md`) — newest entry on top.

Entry format:

```
## YYYY-MM-DD — <commit title>
- Commit: <short hash>
- What changed: <1-3 sentences>
- Why: <the problem/motivation, if not obvious>
- Files: <key files touched>
```

---

## 2026-09-06 — fix(install): add missing indexes on Site Backup and Press Notification
- Commit: `af68687`
- What changed: `after_migrate()` now also calls
  `frappe.db.add_index("Site Backup", ["status", "files_availability",
  "physical", "offsite", "creation"])` and
  `frappe.db.add_index("Press Notification", ["team", "read"])`.
- Why: Slow query log on `saas-restore` (production RDS) showed the
  hourly `expire_local_backups` sweep full-scanning `Site Backup`
  (824,571 rows examined, 0 updated, 30s+) since none of its filter
  columns are indexed -- Press's `on_doctype_update` for this doctype
  only covers `(files_availability, job)`. Same log also showed
  `get_unread_count` full-scanning `Press Notification` (59,861 rows
  examined for 1 row) since neither `team` nor `read` is indexed at all.
  Highest-priority pair of a 7-query review (see
  https://claude.ai/code/artifact/7da9e752-6d61-4dc8-a79a-93c5288273f1);
  remaining queries follow in a separate commit.
- Files: `mazeed_custom_press/install.py`

## 2026-09-06 — fix(install): add missing index on Agent Job.site
- Commit: `497ecf8`
- What changed: `after_migrate()` now also calls
  `frappe.db.add_index("Agent Job", ["site", "creation"])`.
- Why: Slow query log showed `SELECT DISTINCT name FROM tabAgent Job WHERE
  site = ...` doing a full table scan (Full_scan: Yes, Rows_examined:
  19705). Press's `on_doctype_update` for Agent Job only indexes
  `(status, server)`, `(reference_doctype, reference_name)`, and
  `(creation)` -- nothing covers `site`, which is filtered on by the
  dashboard's Site "Jobs" tab and several `frappe.db.exists("Agent Job",
  {"site": ...})` checks in `press/api/site.py`. Fixed here instead of
  patching Press directly.
- Files: `mazeed_custom_press/install.py`

## 2026-08-31 — feat(saas_site): route mazeed_copilot through Phase 1 pooled-site rename
- Commit: `9b790bc`
- What changed: Added `mazeed_copilot` to the app-name check in
  `CustomSaasSite.rename_pooled_site`, so it routes through
  `_rename_pooled_site_erpnext` (Phase 1 only: metadata/config update,
  no subdomain change, no agent rename job) like `erpnext` and
  `mazeed_theme`.
- Why: New Mazeed app needs the same pooled-site claim behaviour as the
  existing custom apps. Note: requires a `Saas Settings` record named
  `mazeed_copilot` (with `site_plan` etc.) to exist for
  `get_saas_site_plan` to resolve a plan — this is a data setup step,
  not covered by this commit.
- Files: `mazeed_custom_press/overrides/saas_site.py`

## 2026-08-07 — feat(proxy-scaling): per-Cluster agent source override for dev-cluster validation
- Commit: `09473a9`
- What changed: Added `Cluster.custom_agent_repository_owner`/`custom_agent_branch`
  custom fields (blank by default) and a new `overrides/agent_source.py` that
  patches `BaseServer.get_agent_repository_url`/`get_agent_repository_branch`
  to check those fields first, falling back to the existing global Press
  Settings values when blank. Registered in `hooks.py`'s `before_request`/
  `before_job`.
- Why: Press Settings only exposes one global `agent_repository_owner`/`branch`,
  applied identically to every `Server`/`Proxy Server` -- there was no way to
  run a modified `agent/` build on a development cluster while production
  kept the default. This is the prerequisite for validating the Phase 1
  proxy-tier scaling plan (`doc/phase1-proxy-tier-plan.md`), which needs a
  dev cluster running modified agent code before any change reaches
  production.
- Files: `mazeed_custom_press/install.py`, `mazeed_custom_press/overrides/agent_source.py`,
  `mazeed_custom_press/hooks.py`, `mazeed_custom_press/tests/test_agent_source.py`

---

## 2026-07-30 — fix(SaasPool/SaasSite): atomic pooled-site claim + earlier commit to stop lock wait timeouts
- Commit: `e92c512`
- What changed: `custom_get` in `overrides/saas_pool.py` now claims a pooled
  standby site via a conditional `UPDATE tabSite SET is_standby = 0 WHERE
  name = %s AND is_standby = 1` over a batch of candidates, instead of a
  plain `SELECT ... LIMIT 1`, returning the first candidate the UPDATE
  actually affects. `_rename_pooled_site_erpnext` in `overrides/saas_site.py`
  now calls `frappe.db.commit()` right after `self.save()`, before
  `create_subscription()` and the `Agent Job` insert run.
- Why: A production traceback showed `QueryTimeoutError` (MySQL 1205, "Lock
  wait timeout exceeded") on an `INSERT INTO tabAgent Job` during
  `new_saas_site`. Root cause: `custom_get`'s unlocked SELECT let two
  concurrent `new_saas_site` calls be handed the *same* pooled site, and
  `new_saas_site` held one long-lived transaction (single commit at the very
  end) across the site save, subscription creation, and Agent Job insert —
  so both racing requests held locks on the same `Site`/`Agent Job` rows
  until one timed out. The conditional UPDATE makes the claim atomic
  (mutual exclusion falls out of the row lock MySQL takes on the UPDATE);
  the earlier commit shrinks the lock-hold window for everything after the
  claim, mirroring the commit-per-site pattern core Press already uses in
  `press/press/doctype/site/saas_pool.py`. Trade-off: if `create_subscription`
  or the Agent Job insert fails after this earlier commit, the site's claim
  is now permanent (won't roll back), matching the trade-off core Press
  already accepts in its own pool-creation commits.
- Files: `mazeed_custom_press/overrides/saas_pool.py`,
  `mazeed_custom_press/overrides/saas_site.py`

---

## 2026-07-21 — fix(Site): skip apps missing from bench instead of throwing
- Commit: `ed66bd9`
- What changed: Added `custom_validate_installed_apps` override for
  `press.press.doctype.site.site.Site.validate_installed_apps` in
  `overrides/site.py`. Previously, if any app in a new site's `apps` list
  wasn't present on the target Bench, `frappe.throw()` aborted the entire
  site creation. Now that app is skipped and logged via
  `log_error("Site App Not On Bench - Skipped", site=..., bench=..., app=...)`
  instead, and the rest of the apps still get created/sorted normally.
- Why: Found while investigating why pooled SaaS sites installed
  `mazeed_feature_flags` before `mazeed_theme` — the reverse of the intended
  order. Root cause traced to `Site.sort_apps()` re-sorting `self.apps` to
  match the target **Bench**'s app order (a snapshot taken from the Deploy
  Candidate at Bench-creation time, not the live Release Group config) on
  every new site insert. The real ordering fix was a data fix — reordering
  apps directly on the active Bench doc's `apps` child table (via console,
  since `Bench.apps` is UI `read_only` and the fix needed to apply
  immediately without a full redeploy). This code change is a hardening
  measure on top of that: it stops a single missing/misconfigured app from
  blocking an entire pooled site creation.
- Files: `mazeed_custom_press/overrides/site.py`
