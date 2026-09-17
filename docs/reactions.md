# Chat reactions

`/reactions` (also `/реакции`) opens a chat-local leaderboard. Its buttons switch
between receivers, givers, popular messages and a summary with popular emoji.
The rolling windows are 24 hours, seven days and thirty days. Results stay in one
message, and message links lead back to the original chat or forum topic.

## Scores and coverage

One identified person adding an ordinary reaction to one message earns one point
for giving and, when its human author is known, one point for receiving. Multiple
emoji do not multiply points; changing emoji keeps the point's observed start.
Removing every ordinary reaction removes the point. A later addition starts a
new active period, with at most one current point for that person/message pair.
The bot must observe the transition from no ordinary reaction to at least one;
a first-seen emoji switch cannot establish when an existing reaction began.
Known self-reactions and bot actors are excluded. No sentiment weights are
assigned to particular emoji.

Distinct supporters, recipients and messages supplement the score. Anonymous
totals, reactions made on behalf of chats, and paid reactions are shown
separately; their identities or quantities cannot be compared fairly with one
person's point. Custom emoji retain their identifiers in storage; the summary
groups them under a neutral custom-emoji label.
For these separate totals, the window selects recently changed snapshots;
Telegram does not expose the age of each anonymous or paid reaction within them.

Telegram supplies reaction changes only to chat administrators and only with
an explicit update subscription. Polling requests all update kinds supported by
the installed Bot API client, including both reaction types. This preserves
passive history independently of command registrations. Reaction updates never
enter an FSM conversation and do not produce automatic chat replies.

Tracking starts with observed changes: Telegram offers no reaction-history
backfill and omits reactions made by bots. Anonymous snapshots can be delayed
by minutes. An end-to-end delivery check needs a real user's reaction in an
administrated chat, not `setMessageReaction` called by the bot.

Reaction events contain neither the message author nor its topic. Rankings join
retained, observed messages; a reaction arriving before its message can acquire
correct attribution later. An unknown author receives no invented credit, and
`sender_chat` takes precedence over Telegram's compatibility user. Giver points
without an identified human recipient are reported as unattributed. Expired or
unseen message records cannot establish authorship, and deleted messages may leave broken
links because Telegram does not report ordinary message deletion to bots.

## Persistence

The existing bounded archive pipeline is the sole writer. Receipt deduplication,
entity observation and reaction state changes share one PostgreSQL transaction.
Each actor's selection replaces its previous selection; `(event_at, update_id)`
rejects stale selection changes, including same-second edits. Separate bounded
start/clear watermarks preserve scoring dates when archive workers finish out of
order. Late transition evidence may correct a score without reverting the
current selection. Empty selections remain as bounded tombstones so an older
addition cannot resurrect a removed point.
Anonymous counts replace a complete per-message snapshot rather than adding to
it. Named and anonymous modes are never summed, and an anonymous snapshot
invalidates older named observations for that message. Personal scoring after
that boundary needs a new observed addition; aggregate counts cannot establish
an individual's continued reaction.

| Data | Lifetime |
| --- | --- |
| Actor selections and removal tombstones | Thirty days from their latest accepted Telegram event |
| Anonymous count snapshots | Thirty days from their Telegram event |
| Leaderboard points | Active selections whose original observed active-period start falls within the selected window, at most thirty days |
| Original message bodies and attribution | Existing message retention: thirty days from the original message date |
| Raw update receipts | Existing receipt retention: thirty days from receipt |

Queries enforce expiry even between maintenance runs. Administrative retention
deletes bounded batches; reaction tables have no cascading references into
messages or receipts and do not copy message bodies or create durable mutation
journal entries. User/chat profiles follow their existing durable lifecycle.
The principal-gated API derives bot identity on the server. Handlers derive chat
identity from the actual message; callback payloads cannot select another chat.

The archive drains on orderly shutdown. A hard crash or an outage longer than
Telegram's delivery window can leave gaps; these rankings are observed activity,
not a complete historical ledger. Telemetry records update kind, identifiers and
storage outcomes according to [the privacy contract](observability.md), never
reaction payloads or message text.

Schema and maintenance changes follow [database operations](database-operations.md).
Use isolated SQL tests for replay, ordering, attribution, scope and retention;
then verify the authenticated API and a live human reaction after deployment.
