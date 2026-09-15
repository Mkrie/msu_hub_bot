# MSU Hub Bot

A Russian-speaking Telegram community bot with media tools, OCR, speech and song
recognition, translation, code execution through JDoodle, games, polls, and chat
administration.

The first release preserves the deployed aiogram 2 command set and data formats.
Provider APIs and dependencies are intentionally kept at the observed legacy
baseline. Some external services may have changed since the original code was
written; provider migrations are separate work.

## Development

Use Python 3.11 and uv 0.12.15 or newer. Native development works on macOS; the
ACRCloud native SDK is installed only on Linux x86-64. FFmpeg and Tesseract are
needed for media and OCR commands.

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
cp .env.example .env
```

Fill in `HUB_BOT_TOKEN`, `HUB_REDIS_HOST`, and `HUB_EDGEDB_DSN`, then run:

```sh
uv run --env-file .env msu-hub-bot
```

Only one poller may use a Telegram token at a time. Local tests block network
access and use synthetic data; they require no live bot or database.

Configuration is documented by `.env.example` and the typed settings in
`msu_hub_bot/settings.py`. Lists, tuples, and mappings use JSON. Optional services
stay registered when unconfigured and return an unavailable response when used.
Administrator IDs default to no access. The Redis namespace defaults to `hub`;
changing it disconnects the bot from its existing state.

Real environment files, credentials, logs, dumps, private keys, and core dumps
are excluded from Git and Docker builds. Never put tokens in build arguments or
paste an expanded production Compose configuration into logs or issues.

## CI and production

Pull requests run locked Python checks on Linux and macOS, Ruff, tests, secret
scanning, workflow validation, and a Linux amd64 container smoke test. Actions
references and tool versions are pinned. Production credentials are confined
to the deployment job on the `production` environment.

Main-branch changes run the same checks, build and test the release image,
publish it to private GHCR storage, then deploy its immutable digest to the VPS.
The bot reuses the existing Redis and EdgeDB services on the external `msu_db`
network. Deployment does not run migrations or recreate shared infrastructure.

The initial repository bootstrap leaves the repository variable
`DEPLOY_ENABLED` unset until the host and secrets are ready. Once set to `true`,
main deploys automatically. No staging bot or approval gate is required.

See [deployment operations](docs/deployment.md) for host setup, secrets,
rollback, and moving to another VPS.

## Maintenance and licensing

The launcher preserves the application's existing import layout. Ruff
exceptions are limited to specific legacy files; runtime, deployment, and test
code uses Ruff formatting.

Demotivators use Liberation Serif. Image generation uses an external CAPTCHA
service when configured. The debate dataset and font assets are bundled.

Application license: **GPL-3.0-only**. Fonts retain their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md). ACRCloud binaries remain an
external dependency, and deployment images stay private while their
redistribution terms are reviewed.
