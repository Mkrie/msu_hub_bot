# Building with TeleForge

An application installs feature instances. Each feature owns its related
entrypoints, rendering and operations; its constructor receives shared services.
Explicit invocation contexts keep users, source messages and response targets out
of shared mutable state.

TeleForge uses native aiogram routers, filters and request middleware. Application
stores own persistence and transactions. Resource lifetimes, background work and
delivery outcomes remain explicit so successful work can survive a failed UI
update.

See [AGENTS.md](../AGENTS.md) for development and review rules.
