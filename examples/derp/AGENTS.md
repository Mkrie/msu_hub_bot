# Derp consumer example

- Keep a feature's Telegram entrypoints and its application-service calls together in `features.py`. Provider selection, storage, charging and consent belong to the injected Derp services.
- Native payment facts and inline identities cross the adapter unchanged. Payment acceptance and recovery must remain idempotent in the application service; a job declaration does not create a transaction or retry policy.
- Exercise changes through the offline native dispatcher tests in `tests/test_teleforge_derp.py`. Do not import the research checkout, construct production services or make live requests.
