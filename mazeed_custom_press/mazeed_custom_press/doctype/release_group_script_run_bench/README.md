# Release Group Script Run Bench

This child DocType stores the per-bench status for a single `Release Group Script Run`.

## What it represents

- One requested bench from the parent job.
- The bench-level execution outcome.
- Any skip reason, stdout, stderr, exit code, timeout flag, or unexpected error text.

## Status values

- `Pending`: the parent job was created and the bench has not started yet.
- `Running`: the worker is currently processing this bench.
- `Skipped`: the bench could not be loaded or had no eligible sites.
- `Success`: the script completed successfully for that bench.
- `Failure`: the script exited non-zero, timed out, or raised an error.

## How to read the fields

- `bench`: the requested bench name.
- `skip_reason`: why the bench was not executed.
- `sites`: the JSON list of eligible site names passed to the script.
- `stdout` and `stderr`: raw process output from the bench run.
- `exit_code`: the script exit code when available.
- `timed_out`: whether the bench exceeded its timeout.
- `error`: additional failure details captured by the worker.

## Usage in the parent job

- One row is created for each requested bench.
- The parent job updates these rows as it processes benches sequentially.
- The parent job uses the child rows to build the final CSV result payload.
