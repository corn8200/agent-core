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

## The other four agents (sandbox-extension, 2026-04-18)

Each agent reuses the **same image** (`agent-core/watcher-pilot:latest`) built
by the pilot. Non-pilot compose files have no `build:` block — they only
reference the tag. Only `run-pilot.sh` builds; the four extension wrappers
assume the image already exists.

| Short  | Compose file                          | Wrapper                  | Source mount                     |
|--------|---------------------------------------|--------------------------|----------------------------------|
| pilot  | `docker-compose.pilot.yml`            | `run-pilot.sh`           | `~/Projects/anthropic-update-watcher` |
| longtoken     | `docker-compose.longtoken.yml`     | `run-longtoken.sh`     | `~/bin` (claude-longtoken-check.sh) |
| nightly-infra | `docker-compose.nightly-infra.yml` | `run-nightly-infra.sh` | `~/bin` (nightly-infra-check.sh)    |
| herald        | `docker-compose.herald.yml`        | `run-herald.sh`        | `~/bin` (herald-inbox-launcher.sh)  |
| memory        | `docker-compose.memory.yml`        | `run-memory.sh`        | `~/Projects/agent-core`             |

All five share: non-root `agent` uid 1001, `read_only: true` root FS, `cap_drop:
[ALL]`, `no-new-privileges`, anthropic env scrub, `~/.claude/` + `~/.claude.json`
RW mount for OAuth token refresh, default bridge network, tini init.

Each wrapper accepts arbitrary argv — `./sandbox/run-<short>.sh claude --version`
bypasses the plist's real command via `--entrypoint ""` and proves the mounts +
Max auth work. Bare `./sandbox/run-<short>.sh` runs the service's `command:`.

### Known runtime caveats (sandbox-compat status)

The compose files ship and `claude --version` passes in each. The underlying
LaunchAgent payloads have mount/env requirements beyond what's wired today —
flagged for John's decision before the plist swap:

- **longtoken** — bash script calls `ssh vps` and sources
  `$HOME/.config/load-machine-secrets.sh`. Neither SSH keys nor the legacy
  secrets file are mounted in. Also: does not invoke the Claude CLI at all; the
  sandbox image isn't strictly needed for it.
- **nightly-infra** — uses macOS-only host tools (`memory_pressure`, `sysctl`,
  `launchctl`, `df -P`). Linux container lacks these. Also does `ssh vps` /
  `ssh pi` and sources the legacy secrets file. Not a Claude CLI caller either.
- **herald** — imports `core.vault` (1P hydrate), reads
  `~/Projects/brand-kit/qc-rules.md` + `tokens/design-tokens.json`, writes to
  `~/Projects/brand-kit/reports/`. Needs `brand-kit` mounted RO + `reports/`
  RW. Also pulls `pymupdf` (mechanical-tier PDF check) which is **not** in the
  pilot Dockerfile — container would ModuleNotFoundError on real invocation.
  Rebuild required if herald runs its full code path in-sandbox.
- **memory** — pure Python pattern-detector over `~/logs/agent-memory.db` +
  Pushover. Does not invoke the Claude CLI. Needs `~/logs/` RW mount to read
  the db and `core.vault` (which tries 1P service-account token at
  `~/.config/op-service-account-token`). Legacy fallback `~/.config/secrets.env.legacy`
  also not mounted. Non-Claude consumer.

### What the pilot sandbox proves vs. what it doesn't

- **Proven:** OAuth token mount works, CLI runs, caller-env scrub works, rootfs
  + cap_drop + no-new-priv hold.
- **Not yet proven for the four extensions:** the agent's **real** command
  succeeding inside the container. Each of the four needs further
  mount/dep work before the LaunchAgent plist can be swapped.
