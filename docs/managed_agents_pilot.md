# Managed Agents pilot

`core.managed` is a disabled-by-default adapter for Anthropic's raw
API-billed Managed Agents beta. Importing the module and running
`managed_agents_capability_probe()` are local-only: they do not use credentials,
create hosted resources, or make network requests.

The production path requires both an explicit per-process opt-in and a billing
credential:

```sh
AGENT_CORE_MANAGED_AGENTS_ENABLED=1 \
ANTHROPIC_CONSOLE_KEY=... \
python -c 'import asyncio; from core.managed import managed_query; print(asyncio.run(managed_query("...")))'
```

Keep the opt-in unset in long-running services. A bounded pilot should use a
dedicated credential, fixed prompt, single session, and verify that the session
is archived. Disable the path by removing
`AGENT_CORE_MANAGED_AGENTS_ENABLED` from the pilot process; no config or service
restart is required.

The local test pilot exercises environment, agent, session, event-stream, and
archive behavior through a fake transport. It intentionally proves the adapter
without creating billable hosted resources.
