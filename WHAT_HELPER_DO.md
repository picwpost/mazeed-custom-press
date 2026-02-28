# Mazeed Custom Press: What It Does

This file documents the current overrides and customizations implemented by `mazeed_custom_press`.

## 1) Hook-Level Overrides

Defined in `apps/mazeed_custom_press/mazeed_custom_press/hooks.py`.

- `override_whitelisted_methods`
  - Replaces:
    - `press.api.saas.new_saas_site`
  - With:
    - `mazeed_custom_press.api.saas.new_saas_site`

- `before_request`
  - Applies runtime monkey patches on each request:
    - `mazeed_custom_press.overrides.saas_pool.apply_overrides`
    - `mazeed_custom_press.overrides.saas_site.apply_overrides`

- `before_job`
  - Applies the same runtime patches for background jobs:
    - `mazeed_custom_press.overrides.saas_pool.apply_overrides`
    - `mazeed_custom_press.overrides.saas_site.apply_overrides`

## 2) Saas Pool Runtime Override

Defined in `apps/mazeed_custom_press/mazeed_custom_press/overrides/saas_pool.py`.

- Overrides `SaasSitePool.create_one` at runtime.
- Custom behavior:
  - Builds standby sites for SaaS pools.
  - Sets standby site config key `pause_scheduler` before insert.
  - Retries site creation up to 5 times on `DuplicateEntryError` (subdomain collision).
  - Creates app subscriptions and maps them to the created site.
  - On failure:
    - Logs error via `press.utils.log_error`.
    - Logs traceback via `frappe.log_error`.
    - Sends email alert to `ahmed.abdellatif@mazeed.com`.

## 3) Saas Site Runtime Override

Defined in `apps/mazeed_custom_press/mazeed_custom_press/overrides/saas_site.py`.

- Replaces `press.press.doctype.site.saas_site.SaasSite` with `CustomSaasSite` at runtime.
- Adds helper:
  - `CustomSaasSite.update_configuration(config=None, save=True)`
  - Normalizes incoming config payload (dict/list/json-string) then calls `_update_configuration`.

## 4) SaaS API Override

Defined in `apps/mazeed_custom_press/mazeed_custom_press/api/saas.py`.

- API method:
  - `mazeed_custom_press.api.saas.new_saas_site`
- Behavior:
  - Creates or reuses pooled site using `CustomSaasSite`.
  - Supports incoming `config` payload in multiple shapes.
  - Applies config with `site.update_site_config(...)` so changes propagate to real server config (agent job), not only DB preview.
  - Commits DB transaction before return.

## 5) New Custom DocType

DocType files:
- `apps/mazeed_custom_press/mazeed_custom_press/mazeed_custom_press/doctype/release_group_branchs/release_group_branchs.json`
- `apps/mazeed_custom_press/mazeed_custom_press/mazeed_custom_press/doctype/release_group_branchs/release_group_branchs.py`
- `apps/mazeed_custom_press/mazeed_custom_press/mazeed_custom_press/doctype/release_group_branchs/release_group_branchs.js`

DocType name:
- `Release Group Branchs`

Fields:
- `release_group` (Link -> `Release Group`)
- `user` (Link -> `User`)
- `mazeed_theme_branch` (Data)
- `feature_flag_branch` (Data)

Layout:
- `release_group` and `user` are on the same row (using `Column Break`).

Validation rules (`release_group_branchs.py`):
- At least one branch is required:
  - `mazeed_theme_branch` OR `feature_flag_branch`.
- Duplicate prevention:
  - Cannot save duplicate tuple:
    - `(release_group, mazeed_theme_branch, feature_flag_branch)`.

## 6) Custom APIs for Release Group Branchs

Defined in `apps/mazeed_custom_press/mazeed_custom_press/api/release_group_branchs.py`.

- Create:
  - `mazeed_custom_press.api.release_group_branchs.new`
  - Creates one `Release Group Branchs` document.

- Fetch:
  - `mazeed_custom_press.api.release_group_branchs.get`
  - Supports:
    - `name` for single doc fetch.
    - Filtered list by any combination of:
      - `release_group`
      - `user`
      - `mazeed_theme_branch`
      - `feature_flag_branch`
    - Pagination:
      - `limit`
      - `start`
  - Filters are combined with `AND`.

## 7) Access Control Pattern

The custom APIs are restricted to:
- `System Manager` (via `frappe.only_for("System Manager")`).

