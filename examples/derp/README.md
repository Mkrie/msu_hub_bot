# Derp with TeleForge

This executable consumer example keeps Telegram entrypoints beside ordinary application-service calls. `Assistant` owns text and inline use, `Images` owns creation and editing, and `Commerce` owns payment events and recovery registration. None imports a bot checkout or selects a provider, database, price, billing workflow or consent policy.

Construct the application with `create_app(answers, images, payments)`. Supply Derp's actual services through those three protocols. They receive native photo/payment objects and explicit actor/request identity. `App.build_router()` embeds the features in an existing dispatcher; `App` also supports standalone lifecycle and dispatch.

Register recovery handlers with `bind_jobs(app, worker)`. The durable name is `derp.commerce.recover-payment`, from the feature key and declared job name; renaming the Python method does not rename persisted work. The application service must atomically accept a payment and enqueue its recovery payload in its own transaction. The worker owns claims, retry decisions and execution context. Repeated native payment updates and repeated job delivery reach the service; TeleForge does not pretend to make them exactly-once. The test double demonstrates this ownership boundary with in-memory state, not a production database implementation.

Inline answers edit the existing inline message and cannot invent a destination chat or split the result into new messages. Image input can remain a native `PhotoSize`, so this adapter does not download or decode it. Long-running authorization, generation or recovery remains a service responsibility; a feature can expose its state through optional managed cards without fabricating an LLM tool transcript.

Run the consumer proof with:

```sh
uv run --no-sync pytest -q tests/test_teleforge_derp.py
```
