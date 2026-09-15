# Deployment operations

## Host contract

The application uses `~/msu_hub_bot` on the deployment host. The SSH deployment
key is separate from personal SSH keys and is restricted to `deploy/deploy.py`
as a forced command. The wrapper accepts only deploy/rollback JSON requests,
validates the image repository and digest, and uses a host lock.

Requirements: Linux x86-64, Python 3, Docker, Compose 2.30 or later, and access to
the existing Redis/EdgeDB services through Docker network `msu_db`. The account
must be able to use Docker. Passwordless sudo is not required.

Install the reviewed wrapper as `~/msu_hub_bot/deploy.py` in a mode-700 directory.
Add a dedicated Ed25519 public key to the deployment account's authorized keys:

```text
restrict,command="/usr/bin/python3 /home/DEPLOY_USER/msu_hub_bot/deploy.py" ssh-ed25519 PUBLIC_KEY msu_hub_bot-actions
```

Use the actual home path when moving hosts. Wrapper changes require the normal
administrator SSH connection; the restricted CI key cannot upload executable
scripts or run arbitrary commands. Keep the deployment directory and SSH files
private to their owner.

## GitHub configuration

Create a `production` environment permitting deployments from `main` only.
Required reviewers are intentionally not enabled.

| Setting | Storage |
| --- | --- |
| `HUB_*` application configuration from `.env.example` | Production environment secrets |
| `DEPLOY_SSH_KEY` | Production environment secret; dedicated private deployment key |
| `SSH_HOST`, `SSH_USER`, `SSH_PORT` | Production environment secrets |
| `SSH_KNOWN_HOSTS` | Production environment secret; independently verified host keys |
| `DEPLOY_ENABLED=true` | Repository variable; enable after bootstrap |

GitHub is the source of truth for runtime settings. For example, set an updated
value through stdin with `gh secret set HUB_BOT_TOKEN --env production --repo
uburuntu/msu_hub_bot`, then dispatch the Deploy workflow. Do not place secret
values in `--body` arguments or shell history. Arrays and mappings must be JSON.

The workflow sends configuration over authenticated SSH. The wrapper stores a
mode-600 `runtime.env` for each release, outside any checkout. Its single JSON
envelope is read using Compose's raw environment-file mode, preserving quotes,
dollar signs, and multiline values without interpolation. Registry login uses
the job's short-lived token in a temporary Docker configuration directory that
is deleted after the pull. No permanent registry PAT is required.

## Cutover and rollback

Before stopping the current bot, the wrapper pulls the new image and runs a
separate preflight: configuration validation, Redis ping, EdgeDB `SELECT 1`,
Telegram `getMe`, and required media programs. Preflight never polls Telegram,
sends messages, or migrates the database.

When an existing `hub_bot` container is present, the first cutover retains it,
disables its restart policy, and explicitly
signals its Python process because the old shell entrypoint does not forward
signals. It stops the old container before starting `msu_hub_bot`. Subsequent
deployments also stop the current poller before starting its replacement.

Successful polling updates a readiness heartbeat. A release has five minutes
to become healthy; shutdown has a 90-second allowance. Failure stops the new
poller before restoring the previous image and configuration. Initial rollback
restores that container and its original restart policy. A host without an
existing container can deploy directly; manual rollback becomes available
after a second successful release.

Use **Actions → Rollback → Run workflow** from main to restore the preceding
release. `current.json` and `previous.json` record revision, digest, and release
directory. Stored runtime configuration is sensitive; do not attach these
directories to issues or CI artifacts. Container logs are rotated locally.

## Moving VPSs

Provision Docker and the required database connectivity, install the reviewed
wrapper and a new restricted key, verify the new host key independently, and
update the production SSH secrets. Runtime application secrets
stay in GitHub. Stop the old host's bot before activating the new one.

The initial migration deliberately keeps the existing data stores. Moving or
restoring Redis/EdgeDB, changing TLS trust, and applying schema migrations are
separate operations. The retained `dbschema` directory is reference material;
deployment never applies it automatically.
