# TeleForge features

- Keep Telegram entrypoints, cards and named steps together; reuse bot services and presentation helpers instead of duplicating business rules.
- Constructor dependencies live as long as the feature. Request state belongs in typed contexts or application-owned persisted records.
- The host owns its router order, middleware, storage and transactions. Build isolated feature routers for dispatch tests; changing production composition requires deliberate replacement of the corresponding legacy routes.
- Preserve aliases, reply targeting, topic scope, Russian copy, authorization and exact-preview barriers. Native Telegram escape hatches are appropriate when their semantics matter.
