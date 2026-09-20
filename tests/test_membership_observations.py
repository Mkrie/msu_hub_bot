"""Membership evidence stays separate from activity and Telegram proxy senders."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    Update,
)

from msu_hub_bot.storage.observations import archive_observation, membership_batch

NOW = datetime(2026, 9, 20, tzinfo=UTC)
CHAT = {"id": -1001, "type": "supergroup", "title": "Synthetic"}
USER = {"id": 42, "is_bot": False, "first_name": "Synthetic"}


def message(**fields):
    return {"message_id": 10, "date": NOW, "chat": CHAT, "from": USER, **fields}


@pytest.mark.parametrize("kind", ["chat_member", "my_chat_member"])
@pytest.mark.parametrize(
    "member_type,is_member",
    [
        (ChatMemberOwner, None),
        (ChatMemberAdministrator, None),
        (ChatMemberMember, None),
        (ChatMemberLeft, None),
        (ChatMemberBanned, None),
        (ChatMemberRestricted, True),
        (ChatMemberRestricted, False),
    ],
)
def test_member_status_variants_preserve_subject_permissions_and_event_order(kind, member_type, is_member):
    fields = {name: False for name, field in member_type.model_fields.items() if field.annotation is bool}
    fields.update(user=USER, until_date=0)
    if is_member is not None:
        fields["is_member"] = is_member
    member = member_type.model_validate(fields)
    event = {
        "chat": CHAT,
        "from": {**USER, "id": 77},
        "date": NOW - timedelta(seconds=5),
        "old_chat_member": {"status": "left", "user": USER},
        "new_chat_member": member,
    }
    row = archive_observation(Update.model_validate({"update_id": 100, kind: event}), False, received_at=NOW)
    assert len(row.memberships) == 1
    evidence = row.memberships[0]
    assert evidence.user_id == 42
    assert evidence.status == member.status
    assert evidence.status_source == kind
    assert evidence.status_event_id == 100
    assert evidence.status_observed_at == event["date"]
    assert evidence.observation_source == "membership"
    assert "user" not in evidence.permissions
    if is_member is not None:
        assert evidence.permissions["is_member"] is is_member


def test_new_activity_does_not_erase_older_join_found_in_reply():
    joined = NOW - timedelta(hours=1)
    event = message(reply_to_message=message(message_id=3, date=joined, new_chat_members=[USER]))
    evidence = archive_observation(Update.model_validate({"update_id": 101, "message": event}), True, received_at=NOW).memberships[0]
    assert evidence.observed_at == NOW
    assert evidence.observation_source == "message"
    assert evidence.status == "member"
    assert evidence.status_observed_at == joined
    assert evidence.status_source == "service_join"
    assert evidence.status_event_id == 3


def test_same_second_self_reply_preserves_direct_activity_source():
    event = message(message_id=11, reply_to_message=message(message_id=10))
    row = archive_observation(Update.model_validate({"update_id": 111, "message": event}), True, received_at=NOW)
    assert row.memberships[0].observation_source == "message"


def test_reading_or_editing_service_notice_cannot_make_its_status_newer():
    joined = NOW - timedelta(days=1)
    service = message(message_id=3, date=joined, edit_date=int(NOW.timestamp()), new_chat_members=[USER])
    snapshots = []
    for update_id in (100, 1000):
        event = message(reply_to_message=service)
        row = archive_observation(Update.model_validate({"update_id": update_id, "message": event}), True, received_at=NOW)
        snapshots.append(row.memberships[0])
    assert all(item.status_observed_at == joined and item.status_event_id == 3 for item in snapshots)


def test_same_second_nested_service_notices_use_message_order():
    # The older leave is visited later, so traversal order must not decide status.
    leave = message(message_id=3, left_chat_member=USER)
    joined = message(message_id=4, new_chat_members=[USER], reply_to_message=leave)
    event = message(message_id=5, reply_to_message=joined)
    row = archive_observation(Update.model_validate({"update_id": 102, "message": event}), True, received_at=NOW)
    assert row.memberships[0].status == "member"
    assert row.memberships[0].status_event_id == 4


def test_callback_actor_is_only_a_candidate_and_old_message_keeps_its_date():
    event = {
        "id": "synthetic",
        "chat_instance": "synthetic",
        "from": USER,
        "data": "button",
        "message": message(date=NOW - timedelta(days=5), **{"from": {**USER, "id": 77}}),
    }
    row = archive_observation(Update.model_validate({"update_id": 103, "callback_query": event}), True, received_at=NOW)
    candidates = {item.user_id: item for item in row.memberships}
    assert candidates[42].observation_source == "callback" and candidates[42].observed_at == NOW
    assert candidates[77].observation_source == "reply" and candidates[77].observed_at == event["message"]["date"]
    assert all("status" not in item.model_fields_set for item in candidates.values())


def test_join_request_does_not_confirm_membership():
    requested = NOW - timedelta(minutes=5)
    event = {"chat": CHAT, "from": USER, "user_chat_id": 12345, "date": requested}
    row = archive_observation(Update.model_validate({"update_id": 104, "chat_join_request": event}), False, received_at=NOW)
    evidence = row.memberships[0]
    assert evidence.user_id == 42 and evidence.observation_source == "join_request"
    assert evidence.observed_at == requested
    assert "status" not in evidence.model_fields_set


def test_anonymous_or_channel_sender_is_not_a_member_candidate():
    event = message(sender_chat=CHAT)
    row = archive_observation(Update.model_validate({"update_id": 105, "message": event}), False, received_at=NOW)
    assert row.memberships == []
    assert row.messages[0].sender_chat_id == CHAT["id"]


def test_inbox_contains_direct_status_evidence_without_bodies_profiles_or_unrelated_users():
    event = message(
        text="BODY_CANARY",
        new_chat_members=[USER],
        reply_to_message=message(new_chat_members=[{**USER, "id": 77}], text="REPLY_CANARY"),
    )
    update = Update.model_validate({"update_id": 106, "message": event})
    batch = membership_batch(update, received_at=NOW)
    assert batch is not None
    assert {item.user_id for item in batch.users} == {42}
    assert {item.user_id for item in batch.memberships} == {42}
    assert all(item.profile == {} for item in batch.chats + batch.users)
    assert "CANARY" not in batch.model_dump_json()
    assert set(batch.model_dump()) == {"update_id", "received_at", "users", "chats", "memberships"}


def test_reply_to_a_join_notice_does_not_create_an_inbox_event():
    event = message(reply_to_message=message(new_chat_members=[USER]))
    assert membership_batch(Update.model_validate({"update_id": 107, "message": event}), received_at=NOW) is None


def test_unrelated_unserializable_message_extra_cannot_block_the_inbox():
    event = message(new_chat_members=[USER], unknown_field=object())
    batch = membership_batch(Update.model_validate({"update_id": 109, "message": event}), received_at=NOW)
    assert batch is not None and batch.memberships[0].status == "member"


def test_inbox_does_not_silently_truncate_large_join_events():
    event = message(new_chat_members=[{**USER, "id": number} for number in range(1, 513)])
    batch = membership_batch(Update.model_validate({"update_id": 110, "message": event}), received_at=NOW)
    assert batch is not None and len(batch.memberships) == len(batch.users) == 512
    event["new_chat_members"].append({**USER, "id": 513})
    with pytest.raises(ValidationError):
        membership_batch(Update.model_validate({"update_id": 111, "message": event}), received_at=NOW)


def test_bot_admin_loss_is_retained_as_coverage_evidence():
    fields = {name: False for name, field in ChatMemberAdministrator.model_fields.items() if field.annotation is bool}
    admin = ChatMemberAdministrator.model_validate({**fields, "user": USER})
    event = {
        "chat": CHAT,
        "from": {**USER, "id": 77},
        "date": NOW,
        "old_chat_member": admin,
        "new_chat_member": {"status": "left", "user": USER},
    }
    for kind in ("my_chat_member", "chat_member"):
        batch = membership_batch(Update.model_validate({"update_id": 108, kind: event}), received_at=NOW)
        assert batch is not None
        assert batch.memberships[0].admin_lost_at == (NOW if kind == "my_chat_member" else None)
