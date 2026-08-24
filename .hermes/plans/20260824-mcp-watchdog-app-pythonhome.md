# MCP stdio servers die at boot under .app-bundled gateway interpreters

**Date:** 2026-08-24 · **Agent:** Doc (self health check) · **Status:** fix landed on local branch, awaiting Chief's yes to apply live

## Symptom

Every ~5 minutes, all three configured MCP servers (`gbrain`, `fleet-health`,
`fleet-skills`) log:

```
WARNING tools.mcp_tool: MCP server '<name>' failed initial connection after 3
attempts, parking until a reconnect is requested: McpError: Connection closed
```

`logs/mcp-stderr.log` shows the real failure, repeated for each server:

```
Could not find platform independent libraries <prefix>
...
  program name = '/Users/agents/Tools/tcc-identity/apps/Doc.app/Contents/MacOS/Doc'
  sys.prefix = '/install'
Fatal Python error: init_fs_encoding: failed to get the Python codec of the
filesystem encoding
ModuleNotFoundError: No module named 'encodings'
```

Started 2026-08-23 ~14:47, right after `ensure-named-gateway-apps.sh`
regenerated the per-agent `.app` bundles at 14:34.

## Root cause chain

1. The gateway LaunchAgent sets `HERMES_GATEWAY_PYTHON` to the
   tcc-identity `.app` wrapper binary and boots it with
   `PYTHONHOME=<uv cpython prefix>` + `PYTHONPATH=tcc-identity/bootstrap`.
   A bare copied CPython has no `pyvenv.cfg`, so it needs those to find its
   stdlib. This works for the gateway itself.
2. `bootstrap/sitecustomize.py` then deliberately pops `PYTHONHOME`,
   `PYTHONPATH`, `FLEET_GATEWAY_SITE_PACKAGES` from `os.environ` so spawned
   children don't inherit a wrong-home env. Correct for normal children.
3. But `tools/mcp_tool.py::_wrap_command_with_watchdog()` spawns every stdio
   MCP server as `sys.executable tools/mcp_stdio_watchdog.py …`. With the
   `.app` interpreter, that child is another bare CPython with **no**
   `PYTHONHOME` → path config falls back to build-time prefix `/install`
   (doesn't exist) → fatal before any code runs.
4. `_build_safe_env()` strips `PYTHONHOME` from child envs by design
   (secret/isolation hygiene), so even if it survived sitecustomize it
   would not reach MCP children.

Net effect: since Aug 23, Doc has had zero working MCP servers from the
gateway (GBrain, fleet-health receipts, fleet-skills). Standalone probes of
the same servers work fine, which is why most checks still pass.

## Proof

Reproduced both directions with the exact binaries:

```bash
# Fails exactly like production:
env -i "$DOCAPP" tools/mcp_stdio_watchdog.py --ppid $$ -- /bin/echo hi
#   -> Fatal Python error: init_fs_encoding ... No module named 'encodings'

# Works with bootstrap env present:
env -i PYTHONHOME="$UV_BASE" PYTHONPATH="$BOOTSTRAP" \
    FLEET_GATEWAY_SITE_PACKAGES="$VENV/site-packages" \
    "$DOCAPP" tools/mcp_stdio_watchdog.py --ppid $$ -- /bin/echo hi
#   -> hi
```

Timeline evidence: first `Fatal Python error` block in mcp-stderr.log follows
a start banner stamped `2026-08-23 14:47:16`; apps were built 14:34 same day.

## Fix (branch `doctor/mcp-watchdog-app-pythonhome-20260824`)

In `tools/mcp_tool.py::_wrap_command_with_watchdog()`: when `sys.executable`
lives inside a `.app` bundle (`*.app/Contents/MacOS/*`), spawn the watchdog
via `<sys.base_prefix>/bin/python3` instead — an out-of-bundle interpreter
that locates its own stdlib without env help. Falls back to the current
behavior when no such sibling exists. Non-macOS / non-bundled case untouched,
so this is invisible everywhere except broken-bundle installs.

Regression tests: `tests/test_mcp_watchdog_app_bundle.py` (13 tests) covers
bundled rewriting, non-bundled passthrough, true python3-over-python preference,
non-executable-sibling fallthrough, one-shot warning latch, case-insensitive
bundle detection, argv shape, and non-POSIX noop. `HERMES_TEST_REAL_BOOT=1`
adds the real-boot check: boots the watchdog via `<base_prefix>/bin/python3`
under a fully stripped env and asserts clean exit + output — pinning the exact
incident failure mode (child interpreter boot), which mocked tests cannot
catch.

## Review trail

Adversarial review (2026-08-24, opencode-zen/x-preview-f-free advisor):
REQUEST_CHANGES adopted. Blocking 3 addressed by real-boot test + plan
correction; blocking 1/2 resolved by closing PR #24 in favor of upstream
41447a6d70 which already carries the stronger `_expand_candidate_path`
machinery. Nonblocking items (preference-order pin, case-insensitive match,
warning rate-limit, boundary doc) folded into this branch. Second independent
review lane (opencode/big-pickle) ran concurrently; synthesis by Doc.

## Operator options

1. **Apply the branch** (my recommendation): point the live install at the
   fix, restart gateway (needs your yes per standing rule), watch mcp-stderr.log.
2. **Env-only workaround**: add `PYTHONHOME=<uv prefix>` back into each
   server's `env:` block in config.yaml — but that poisons non-Hermes pythons
   (uv, brew) the moment they're spawned by any MCP tool. Not recommended.
3. **Revert the .app launcher**: set `HERMES_GATEWAY_PYTHON` back to the plain
   venv python; loses avatar-in-Dock identity polish but restores MCP today.

## Verification plan after apply

- `tail -f logs/mcp-stderr.log` shows `Starting GBrain MCP server (stdio)`
  and no `init_fs_encoding` fatals across two probe cycles (~10 min).
- `fleet_probes.py --agent doc --check gbrain --json` → ok with fresh receipt.
