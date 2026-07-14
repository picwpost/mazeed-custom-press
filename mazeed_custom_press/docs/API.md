# Mazeed Custom Press — API Reference

All endpoints are Frappe whitelisted methods. Call them via:

```
POST https://<your-press-host>/api/method/<endpoint>
Authorization: token <api_key>:<api_secret>
Content-Type: application/json
```

---

## SaaS Sites

### `new_saas_site`

**Endpoint:** `mazeed_custom_press.api.saas.new_saas_site`  
**Access:** System Manager  
**Override of:** `press.api.saas.new_saas_site`

Creates a new SaaS site. Uses a standby pool site if one is available, otherwise provisions fresh. Propagates the optional `config` dict into site configuration.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `subdomain` | string | yes | Subdomain for the new site |
| `app` | string | yes | Marketplace app name |
| `config` | dict \| list | no | Initial site configuration (see formats below) |

`config` accepts three formats:
```json
// dict
{"key1": "value1", "key2": "value2"}

// list of key/value objects
[{"key": "key1", "value": "value1"}, {"key": "key2", "value": "value2"}]

// list of single-key dicts
[{"key1": "value1"}, {"key2": "value2"}]
```

**Request**
```json
{
  "subdomain": "acme-corp",
  "app": "erpnext",
  "config": {"country": "SA", "currency": "SAR"}
}
```

**Response** — Site document
```json
{
  "name": "acme-corp.frappe.cloud",
  "status": "Pending",
  "bench": "bench-abc123",
  "team": "team-xyz"
}
```

**Errors**

| Code | Message |
|---|---|
| 417 | Subdomain already taken (retried up to 5 times) |
| 403 | Requires System Manager role |

---

### `get_standby_site_for_release_group`

**Endpoint:** `mazeed_custom_press.api.saas.get_standby_site_for_release_group`  
**Access:** System Manager

Finds the latest active Bench in a Release Group and returns the oldest active standby site on that bench that has not yet completed the Setup Wizard.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `release_group` | string | yes | Release Group name or title |

**Request**
```json
{
  "release_group": "My ERPNext Group"
}
```

**Response**
```json
{
  "name": "standby-48291034.frappe.cloud",
  "bench": "bench-abc123",
  "status": "Active",
  "setup_wizard_complete": 0
}
```

**Errors**

| Code | Message |
|---|---|
| 417 | `Release Group '<value>' not found.` |
| 417 | `No active bench found for Release Group '<name>'.` |
| 417 | `No available standby site on bench '<bench>'.` |
| 403 | Requires System Manager role |

---

### `send_setup_wizard_to_standby_site`

**Endpoint:** `mazeed_custom_press.api.saas.send_setup_wizard_to_standby_site`  
**Access:** System Manager

Fetches the first ready standby site for a Release Group (same logic as `get_standby_site_for_release_group`) and runs its full Setup Wizard via `frappe.desk.page.setup_wizard.setup_wizard.setup_complete`. Logs in to the live site using the Press agent-based `get_login_sid()` (bypasses the legacy `/?cmd=login` endpoint which fails on wizard-pending sites), then POSTs the wizard payload directly with `requests`.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `release_group` | string | yes | Release Group name or title |
| `args` | dict | yes | Full Setup Wizard payload (see below) |
| `config` | list | no | Site config entries to apply before the wizard — array of `{key, value, type}` objects |

`args` shape:

| Key | Description |
|---|---|
| `language` | Language name (e.g. `"English"`) |
| `country` | Country name (e.g. `"United Arab Emirates"`) |
| `timezone` | IANA timezone (e.g. `"Asia/Dubai"`) |
| `currency` | Currency code (e.g. `"AED"`) |
| `full_name` | Administrator full name |
| `email` | Administrator email address |
| `password` | Administrator password |
| `company_name` | Company name |
| `company_abbr` | Company abbreviation |
| `domain` | ERPNext domain slug (e.g. `"retail_ecommerce"`) |
| `chart_of_accounts` | Chart of accounts template (e.g. `"Standard"`) |
| `usage_goal` | Usage goal slug (e.g. `"generate_sales"`) |
| `fy_start_date` | Fiscal year start (`YYYY-MM-DD`) |
| `fy_end_date` | Fiscal year end (`YYYY-MM-DD`) |

**Request**
```json
{
  "release_group": "My ERPNext Group",
  "args": {
    "language": "English",
    "country": "United Arab Emirates",
    "timezone": "Asia/Dubai",
    "currency": "AED",
    "full_name": "admin",
    "email": "admin@example.com",
    "password": "Qweqwe@123",
    "company_name": "Mazeed",
    "company_abbr": "mz",
    "domain": "retail_ecommerce",
    "chart_of_accounts": "Standard",
    "usage_goal": "generate_sales",
    "fy_start_date": "2026-01-01",
    "fy_end_date": "2026-12-31"
  },
  "config": [
    {"key": "billing_site_url", "value": "/app", "type": "String"},
    {"key": "stripe_customer",  "value": "cus_Ud9wBr6i4mJygo", "type": "String"},
    {"key": "stripe_plan",      "value": "Advance",            "type": "String"}
  ]
}
```

**Response**
```json
{
  "site": "standby-48291034.frappe.cloud",
  "bench": "bench-abc123"
}
```

**Notes**
- If `config` is provided it is applied via a Press agent job (async write to `common_site_config.json`) before the wizard HTTP call is made.
- The wizard call blocks until `setup_complete` returns (may take up to 120 s for large installs).
- If `setup_wizard_complete` is already `1` on the Press side, the wizard call is skipped.
- `setup_wizard_complete` is set to `1` on the Press `Site` record only after the wizard call succeeds.

**Errors**

| Code | Message |
|---|---|
| 417 | `Release Group '<value>' not found.` |
| 417 | `No active bench found for Release Group '<name>'.` |
| 417 | `No available standby site on bench '<bench>'.` |
| 417 | `Could not connect to site '...' to run the setup wizard: <reason>` |
| 403 | Requires System Manager role |

---

## Release Group Branches

### `release_group_branchs.new`

**Endpoint:** `mazeed_custom_press.api.release_group_branchs.new`  
**Access:** System Manager

Creates a `Release Group Branchs` record linking a user to a set of Git branches for a Release Group.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `release_group` | string | yes | Release Group name |
| `user` | string | yes | Frappe user email |
| `mazeed_theme_branch` | string | no | Git branch for Mazeed theme |
| `feature_flag_branch` | string | no | Git branch for feature flags |

**Request**
```json
{
  "release_group": "rg-erpnext-prod",
  "user": "ahmed@mazeed.com",
  "mazeed_theme_branch": "theme/dark-mode",
  "feature_flag_branch": "flags/beta-2026"
}
```

**Response** — Full document dict
```json
{
  "name": "RGB-00001",
  "release_group": "rg-erpnext-prod",
  "user": "ahmed@mazeed.com",
  "mazeed_theme_branch": "theme/dark-mode",
  "feature_flag_branch": "flags/beta-2026",
  "creation": "2026-06-23 10:00:00",
  "modified": "2026-06-23 10:00:00"
}
```

---

### `release_group_branchs.get`

**Endpoint:** `mazeed_custom_press.api.release_group_branchs.get`  
**Access:** System Manager

Fetch a single record by name, or list with optional filters.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `name` | string | no | Document name — returns a single record if provided |
| `release_group` | string | no | Filter by Release Group |
| `user` | string | no | Filter by user |
| `mazeed_theme_branch` | string | no | Filter by theme branch |
| `feature_flag_branch` | string | no | Filter by feature-flag branch |
| `limit` | int | no | Page size (default `20`) |
| `start` | int | no | Pagination offset (default `0`) |

**Request — fetch single**
```json
{
  "name": "RGB-00001"
}
```

**Response — single**
```json
{
  "name": "RGB-00001",
  "release_group": "rg-erpnext-prod",
  "user": "ahmed@mazeed.com",
  "mazeed_theme_branch": "theme/dark-mode",
  "feature_flag_branch": "flags/beta-2026",
  "creation": "2026-06-23 10:00:00",
  "modified": "2026-06-23 10:00:00"
}
```

**Request — list with filters**
```json
{
  "release_group": "rg-erpnext-prod",
  "limit": 10,
  "start": 0
}
```

**Response — list**
```json
[
  {
    "name": "RGB-00001",
    "release_group": "rg-erpnext-prod",
    "user": "ahmed@mazeed.com",
    "mazeed_theme_branch": "theme/dark-mode",
    "feature_flag_branch": "flags/beta-2026",
    "creation": "2026-06-23 10:00:00",
    "modified": "2026-06-23 10:00:00"
  }
]
```

---

## Release Group Scripts

### `run_release_group_script`

**Endpoint:** `mazeed_custom_press.api.release_group_script.run_release_group_script`  
**Access:** Team-scoped (caller must own the Release Group)

Runs a bash script across all active benches in a Release Group. Returns immediately with a job ID; poll `get_release_group_script_job_detail` for results.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `release_group` | string | yes | Release Group name |
| `script` | string | yes | Bash script to execute |
| `timeout` | int | no | Per-bench timeout in seconds (default `300`) |

**Request**
```json
{
  "release_group": "rg-erpnext-prod",
  "script": "#!/bin/bash\nbench version",
  "timeout": 60
}
```

**Response**
```json
{
  "job": "RGSR-00042"
}
```

---

### `create_release_group_script_job`

**Endpoint:** `mazeed_custom_press.api.release_group_script.create_release_group_script_job`  
**Access:** Team-scoped (caller must own all requested benches)

Runs a bash script on an explicit list of benches (rather than all benches in a Release Group).

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `requested_benches` | list \| string | yes | Bench names — JSON array, comma-separated string, or native list |
| `raw_script` | string | yes | Bash script to execute |
| `timeout` | int | no | Per-bench timeout in seconds (default `300`) |

`requested_benches` accepts:
```json
// JSON array
["bench-abc123", "bench-def456"]

// comma-separated string
"bench-abc123, bench-def456"
```

**Request**
```json
{
  "requested_benches": ["bench-abc123", "bench-def456"],
  "raw_script": "#!/bin/bash\ndf -h",
  "timeout": 120
}
```

**Response**
```json
{
  "job": "RGSR-00043"
}
```

---

### `get_release_group_script_job_detail`

**Endpoint:** `mazeed_custom_press.api.release_group_script.get_release_group_script_job_detail`  
**Access:** Team-scoped (caller must own the job)

Returns the full status and per-bench results for a script run job.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `job_id` | string | yes | Job document name |

**Request**
```json
{
  "job_id": "RGSR-00042"
}
```

**Response**
```json
{
  "job": "RGSR-00042",
  "status": "Success",
  "team": "team-xyz",
  "requested_benches": ["bench-abc123", "bench-def456"],
  "timeout": 300,
  "start": "2026-06-23 10:05:00",
  "end": "2026-06-23 10:05:12",
  "duration": 12.4,
  "result_format": "base64-csv",
  "result_payload": "<base64-encoded CSV>",
  "benches": [
    {
      "bench": "bench-abc123",
      "status": "Success",
      "skip_reason": null,
      "sites": ["site-a.frappe.cloud"],
      "stdout": "frappe 15.0.0\n",
      "stderr": "",
      "exit_code": 0,
      "timed_out": false,
      "error": null
    },
    {
      "bench": "bench-def456",
      "status": "Success",
      "skip_reason": null,
      "sites": ["site-b.frappe.cloud"],
      "stdout": "frappe 15.0.0\n",
      "stderr": "",
      "exit_code": 0,
      "timed_out": false,
      "error": null
    }
  ]
}
```

**Job `status` values:** `Pending` · `Running` · `Success` · `Failure` · `Partial`  
**Bench `status` values:** `Pending` · `Running` · `Success` · `Failure` · `Skipped`
