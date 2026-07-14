# Release Group Script Run

This DocType stores one background job for `POST /server/run-release-group-script`.

## What it represents

- The requested benches for one script run.
- The raw script that was submitted.
- The timeout requested for each bench execution, clamped to `1..3600` seconds.
- The owning team for access control.
- The overall job state and the final result payload.
- Per-bench progress and errors through the child table.

## How it is used

- A request to `POST /server/run-release-group-script` creates this DocType and returns `{ "job": "<id>" }` immediately.
- The worker processes benches sequentially in the background.
- `GET /jobs/<id>` reads the same DocType and returns job status plus the stored result payload.
- Access is team-scoped. Callers outside the owning team are rejected.

## Lifecycle

- `Pending`: created and queued.
- `Running`: the worker is processing benches.
- `Success`: all processed benches completed successfully.
- `Failure`: at least one bench was skipped or failed.

## Result payload

- `result_payload` stores the final CSV as a base64 string.
- The CSV includes one row per requested bench.
- Bench rows preserve stdout, stderr, exit code, timeout state, skip reason, and error text.

## Operational notes

- The benches root defaults to `/home/frappe/benches`.
- Each bench is resolved from that root before execution.
- Unloadable benches are skipped and recorded.
- Sites are filtered before execution to exclude unsafe names, standby sites, and maintenance-mode sites.
- The script runs once per loadable bench with the active sites passed as positional arguments.
