# Derp consumer example

- Keep the simple routing examples in `features.py`; the optional `native.py` bridge imports Derp's concrete handlers and services. Provider selection, storage, charging and consent belong to the application.
- Native payment facts and inline identities cross the adapter unchanged. Payment acceptance and recovery must remain idempotent in the application service; a job declaration does not create a transaction or retry policy.
- Exercise simple routes through `tests/test_teleforge_derp.py`. Validate `native.py` only in an isolated Derp environment with synthetic external edges; the Hub test/import graph must not require Derp. Never construct live provider clients or make production requests.
