# Membership observations

The bot stores the membership evidence it has received, not a complete Telegram participant directory. Ordinary messages, replies, callbacks, reactions and join requests create candidate relationships with `unknown` presence. Channel/anonymous `sender_chat` compatibility senders do not identify a person in the chat.

Explicit membership updates and join/leave service events determine the last known state:

| Evidence | State |
| --- | --- |
| `creator`, `administrator`, `member` | `present` |
| `left`, `kicked` | `absent` |
| `restricted` with boolean `is_member=true` / `false` | `present` / `absent` |
| Missing status, unresolved `restricted`, or an older row without a status timestamp | `unknown` |

Every result retains its status timestamp and source. `present` means present **as of that evidence**; missed events, an offline bot and lost administrator access can make it stale. Activity timestamps are independent and never refresh a membership assertion. No periodic Telegram membership refresh runs.

## Storage and ordering

Migration `009_membership_evidence.sql` adds nullable status clocks/provenance to `chat_users`, preserving all existing rows and permission values. It does not infer historical status dates from `last_seen_at`. The table and membership evidence have no automatic expiry; message/receipt retention remains unchanged.

Status snapshots are ordered by Telegram event time, source strength, then event identifier. At the same instant, `chat_member` and `my_chat_member` beat weaker service join/leave events. Membership updates use the enclosing update ID; service events use the original message ID and original message date, including when an edited or quoted message is observed later. Equal clocks are idempotent. An accepted snapshot replaces status and permissions together, clearing obsolete administrator rights. First/last observation bounds merge separately.

Archive writers without the added fields remain accepted: a supplied status uses the supplied observation time, update ID and source `legacy`, the weakest same-time evidence. A sighting without status cannot clear or refresh an existing status. Existing rows without a status clock remain `unknown` until subsequent explicit evidence, while their original raw status and permissions remain stored.

`observe_memberships_v1(p_observation)` accepts a typed `MembershipBatch`: update ID, receipt time, and at most 512 users, chats and membership observations in each list; the complete JSON request is limited to 1 MiB. It accepts no message bodies, whole updates or archive receipts. It uses the same merge as archival and does not consume the archive update ID, so replay can precede later full archival safely. Replays after receipt retention also cannot regress the status clock.

The authenticated principal supplies the bot identity. `my_chat_member` evidence must concern that bot; its `admin_lost_at` records observed transitions out of administrator/creator status and preserves the latest loss even after later regain. The [database operations contract](database-operations.md) governs applying this migration and updating the installed retention revision guard. Startup requires the health capability `memberships: 1`; apply the schema before deploying its writers. Older readers ignore the added capability.

An administrative backfill may replay retained `chat_member`/`my_chat_member` receipts and explicit join/leave service messages through the same merge. Page source rows in bounded batches within the original bot scope; use the embedded event date/update ID or original service message date/message ID. Preserve administrator-loss transitions from old/new status pairs. Preview counts, retain private cursors and verify the result after application. Missing or expired evidence stays unknown; replay must not invent dates from activity or imply that retained history is complete.

## Durable ingestion

Before a successful `getUpdates` response returns to the dispatcher, its direct membership and service join/leave events are committed to a private SQLite inbox. A local write failure or full inbox fails that poll, so the dispatcher cannot advance its acknowledgement offset for those events. Message bodies, arbitrary profiles and historical replies are excluded from the inbox.

One application-owned worker replays saved batches through `observe_memberships_v1` and removes them only after success. Remote failures retry with bounded backoff; interruption after a remote commit safely replays the same status clocks. The inbox survives releases and rollback in the `msu_hub_bot_membership_inbox` Docker volume at `/data/membership-inbox.sqlite3`. Local development uses `HUB_MEMBERSHIP_INBOX_PATH`, defaulting to `.local/runtime/membership-inbox.sqlite3`.

The journal is bound to one bot ID, uses a private directory/file, and holds at most 10,000 batches or 32 MiB of queued payloads. Never delete it to resolve an outage: inspect the fixed failure diagnostic, restore database access and allow replay. A corrupt or unsupported entry remains saved and needs operator investigation. Routine observations still use ordinary asynchronous archival; the inbox specifically protects direct membership transitions. Telegram's own undelivered-update retention and lost administrator access remain unavoidable coverage limits.

## Reading the roster

`BotRepository.list_chat_members(chat_id, state="present", after_user_id=None, limit=50)` returns a typed `ChatMemberPage`. Supported filters are `present`, `absent`, `unknown`, or `None` for all observed relationships. Pages contain at most 100 rows, ordered by user ID; pass `next_after_user_id` to continue until it is null. The primary key serves unfiltered pagination; a `(chat_id, computed state, user_id)` index serves state-filtered pagination. Concurrent changes can change later pages; pagination is not a frozen snapshot.

Each member includes the observed user profile, status/state, status timestamp/source/event ID, restricted `is_member`, latest observation source and first/last observation timestamps. Treat IDs and profile fields as private application data and never attach them to metric labels. The repository exports only its ordinary aggregate storage diagnostics.

Every page also includes `MembershipCoverage`: the bot's latest observed state/status, timestamp/source, known administrator status and latest observed administrator-loss time. `complete` is always `false`, including while the bot is an administrator. Empty results do not prove an empty chat, and absent bot evidence leaves availability unknown. These RPCs authorize the dedicated bot principal, not an individual Telegram user; any command exposing a roster must apply its own user/chat authorization.

Coverage counts all observed, present, absent and unknown relationships in the target chat, independent of the page filter/cursor. A caller may explicitly request Telegram's current `getChatMemberCount` when a comparison is useful; this reader makes no Telegram requests. Matching counts still do not prove that the stored identities form a complete current roster.
