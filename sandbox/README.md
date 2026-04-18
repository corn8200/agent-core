# Agent Sandbox Pilot

First pilot in the "put all 5 unattended agents in Docker sandboxes" effort.
Target: `anthropic-update-watcher`. If this works, template for the other four.

## Auth strategy (Scout's hybrid, see backlog #100)

- `@anthropic-ai/claude-code` installed fresh via npm inside a Linux container
  (the Mach-O host binary at `/opt/homebrew/bin/claude` can't run on Linux).
- `~/.claude/` bind-mounted **read-write** — the CLI rewrites
  `.credentials.json` on token refresh, so the mount must be writable for
  unattended daily runs to survive past the access-token expiry.
- `~/.claude.json` bind-mounted **read-write** too — separate file (~36KB)
  beside the directory, holds project configs/history, also rewritten by the
  CLI on every run. Without this mount the CLI errors out with `Claude
  configuration file not found`.
- `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_CONSOLE_KEY` forced
  to empty in the container env (they'd route the SDK to pay-as-you-go billing
  and bypass the Max subscription). `run-pilot.sh` also errors out if either is
  set in the caller env — belt-and-braces.
- No secrets baked into the image layer.

## Run it

```bash
cd ~/Projects/agent-core
./sandbox/run-pilot.sh                          # default: claude --version
./sandbox/run-pilot.sh claude --print "2+2?"    # proves Max auth works
./sandbox/run-pilot.sh env                      # confirm no anthropic env leak
```

First run builds the image (~1-2 min). Subsequent runs are instant.

## Hardening in place

- Non-root user `agent` (uid 1001)
- `read_only: true` root FS, tmpfs on `/tmp` and `/home/agent/.npm`
- `cap_drop: [ALL]` + `no-new-privileges`
- Writable bind-mount is scoped to `~/.claude/` only
- App source (`~/Projects/anthropic-update-watcher/`) mounted read-only at `/app`

Default bridge network for this pilot. Network isolation (egress allowlist to
api.anthropic.com + github.com + docs.anthropic.com only) is the next step
before templating — tracked with backlog #100.

## Extending to the other four agents

Each agent gets its own `docker-compose.<agent>.yml` that reuses the same
`sandbox/Dockerfile` image. Swap the `/app` bind-mount source, tweak the
`command:` / entrypoint to run that agent's `watcher.py` equivalent, and add a
matching `run-<agent>.sh` wrapper. The `~/.claude/` mount stays the same on
every one — they all share the OAuth token.
