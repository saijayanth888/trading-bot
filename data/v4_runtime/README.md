# V4 runtime data (host-fallback path)

**Active runtime path:** `user_data/v4_runtime/` — that directory is the
bind-mount the dashboard container actually reads/writes. See
`user_data/v4_runtime/README.md` for the full schema.

This `data/v4_runtime/` directory exists only as a local-dev fallback
for tools that resolve `REPO_ROOT/data/v4_runtime` without the
`USER_DATA_ROOT` env var set. The dashboard always prefers the env-var
path; this fallback should generally stay empty in deployed setups.
