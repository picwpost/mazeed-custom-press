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

## 2026-07-30 — fix(SaasPool/SaasSite): atomic pooled-site claim + earlier commit to stop lock wait timeouts
- Commit: (pending — fill in after commit)
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
