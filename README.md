# Praxys Coach — Claude Code Plugin

Claude Code plugin that surfaces [Praxys Coach](https://www.praxys.run) — a power-based scientific training dashboard for endurance athletes — through MCP tools and skills, so you can ask your training questions directly from your terminal.

> Praxys Coach brings supported training and recovery data together, computes power-based training metrics, and serves them via a web dashboard at praxys.run. This plugin is a thin Claude Code client that lets an agent read and act on that data on your behalf.

## Skills

The plugin exposes 8 skills (auto-discovered when installed):

| Skill | What it does |
|-------|-------------|
| `setup` | Connect account-available data sources, set training base, thresholds, race goal |
| `daily-brief` | Today's training signal (Go / Modify / Rest), recovery, upcoming workouts |
| `training-review` | Multi-week diagnosis: volume, consistency, zone distribution, suggestions |
| `training-plan` | Generate or update a personalized 4-week AI training plan |
| `race-forecast` | Predicted finish time, goal feasibility, required threshold improvement |
| `sync-data` | Trigger or check sync status across connected platforms |
| `science` | Browse and switch training science theories (load, recovery, prediction, zones) |
| `add-metric` | (Developer skill) Scaffold a new training metric end-to-end |

Backed by an MCP server with tools like `get_daily_brief`, `get_race_forecast`, `get_training_review`, `trigger_sync`, and `update_settings`.

### Plan authoring vs. managed delivery

The plugin keeps these operations explicit:

- `save_training_plan` authors canonical future workouts in Praxys. It does not
  directly select or mutate an execution platform.
- `push_training_plan` remains as a backward-compatible alias, but new callers
  should use `save_training_plan`.
- `get_managed_plan_status` reports ownership, delivery state, the 14-day plan,
  and opaque conflict IDs.
- `adopt_managed_plan`, `pause_managed_plan`, `resume_managed_plan`, and
  `leave_managed_plan` control the user's managed-delivery consent.
- `resolve_managed_plan_conflict` accepts only the server-provided
  `accept_target` and `restore_praxys` actions.

When managed delivery is already enabled, Praxys may independently deliver a
newly saved canonical plan under that existing consent. Manual workouts and
workouts from another coach remain untouched.

Adoption and resume require the exact `window.start` and `window.end` from a
fresh `get_managed_plan_status(days=14)` result, preventing approval from being
reused after the UTC review window changes.

## Install

You need an account at [praxys.run](https://www.praxys.run) (free, invitation-based) before the plugin is useful — the plugin is the agent client, not the data source.

In Claude Code:

```
/plugin marketplace add github:dddtc2005/praxys-coach-plugin
/plugin install praxys
```

Then authenticate with the `login` MCP tool. It opens praxys.run in your
browser with opaque, expiring handoff state. After first-party approval, the
plugin exchanges a client-held verifier for a revocable MCP session and caches
it at `~/.praxys/token`; the Praxys account JWT never enters the URL or plugin
process. Use `whoami` to verify the account and `logout` to remove only that
active authentication scope.

### Purpose-bound plan context

Personal plan context is deny-by-default even after MCP login:

1. Call `request_personal_context_access` with one purpose, context kind, and
   `read`, `write`, or both.
2. Open the returned Praxys link and approve the exact short-lived request.
3. Call `complete_personal_context_access`.
4. Use `read_personal_context` for the minimum structured projection or
   `preview_personal_context` to validate one single-use structured draft.
5. Call `revoke_personal_context_access` when finished.

Read grants never return narrative, context IDs, provenance internals, consent
receipts, or encrypted data. Write grants create only a request-scoped preview:
the athlete must re-enter or confirm context in the first-party web or miniapp
plan-context surface before anything durable exists. The plugin cannot approve
its own access, grant AI processing, delete context, or persist conversation
text. Local direct-DB mode resolves the same server-authoritative grant tables
and does not bypass purpose, expiry, revocation, ownership, or single-use
checks.

## Configuration

The plugin defaults to the production backend at `https://api.praxys.run`. Override via environment variables:

| Variable | Purpose |
|----------|---------|
| `PRAXYS_URL` | Override backend API URL |
| `PRAXYS_FRONTEND_URL` | Override the browser-login URL |
| `PRAXYS_LOCAL=1` | Switch into local-development mode (see below) |
| `PRAXYS_PROFILE` | Use an isolated named authentication profile |
| `PRAXYS_TOKEN_PATH` | Explicit token-file override for automation or testing |

### Multiple authentication profiles

The default profile remains backward compatible: it writes
`~/.praxys/token` and reads the legacy `~/.trainsight/token` only when the
modern token is absent.

Set `PRAXYS_PROFILE` when a second MCP server must authenticate as a different
Praxys user. For example, `PRAXYS_PROFILE=dev-test` stores its token and config
under `~/.praxys/profiles/dev-test/` and never falls back to the default or
legacy token. Profile names may contain letters, numbers, underscores, and
hyphens. `PRAXYS_TOKEN_PATH` provides a fully isolated explicit path and also
disables legacy fallback.

```json
{
  "mcpServers": {
    "praxys": {
      "command": "python",
      "args": ["/path/to/praxys-coach-plugin/mcp-server/server.py"]
    },
    "praxys-dev-test": {
      "command": "python",
      "args": ["/path/to/praxys-coach-plugin/mcp-server/server.py"],
      "env": {
        "PRAXYS_PROFILE": "dev-test",
        "PRAXYS_URL": "https://api.praxys.run",
        "PRAXYS_FRONTEND_URL": "https://www.praxys.run"
      }
    }
  }
}
```

Run each server's `login` once, then confirm that `whoami` reports the expected
profile, user ID, and email. `logout` deletes only the selected profile's token
and config.

## Local development

Local mode (`PRAXYS_LOCAL=1`) imports directly from the Praxys Python codebase instead of going over HTTP. This is only useful if you have the (private) main `praxys` repo checked out — the plugin expects to live three directories deep inside that repo (`<praxys>/plugins/praxys/...`). The main repo wires it in as a git submodule at that path.

If you only want to use the plugin against praxys.run, ignore this section — remote mode is the default and needs no setup beyond `login`.

Most local tools read or write the development database directly and do not
need login. `trigger_sync` is the exception because it uses the authenticated
local API for background sync behavior; it reads only the active profile's
token and fails clearly when that scope has none.

## License

MIT — see [LICENSE](LICENSE).
