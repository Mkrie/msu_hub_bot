from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from msu_hub_bot.storage.models import (
    ArchivedUpdate,
    ChatObservation,
    ChatRecord,
    DirectoryPatch,
    ReactionObservation,
    ReactionValue,
    UserRecord,
    VkPatch,
)


@pytest.mark.parametrize("metadata", [{"future": [None, "Тест"]}, "{}", [], None, 7])
def test_records_preserve_legacy_json_shapes_and_source_identity(metadata):
    row = ChatRecord(id=UUID(int=1), created="2019-01-02T03:04:05.123456Z", chat_id=-(2**52), type="group", metadata=metadata)
    assert row.metadata == metadata
    assert row.id == UUID(int=1)
    assert row.created == datetime(2019, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    assert row.chat_id == -(2**52)


def test_sparse_patches_and_observations_distinguish_absence_from_clear():
    assert VkPatch().model_dump(exclude_unset=True) == {}
    assert VkPatch(description=None).model_dump(exclude_unset=True) == {"description": None}
    assert DirectoryPatch(pinned_message_id=None).model_dump(exclude_unset=True) == {"pinned_message_id": None}
    sparse = ChatObservation(chat_id=1, type="private")
    cleared = ChatObservation(chat_id=1, type="private", username=None)
    assert "username" not in sparse.model_dump(exclude_unset=True)
    assert cleared.model_dump(exclude_unset=True)["username"] is None


@pytest.mark.parametrize(
    "patch", [lambda: VkPatch(last_post_id=None), lambda: VkPatch(with_header=None), lambda: DirectoryPatch(name=None)]
)
def test_required_fields_cannot_be_cleared(patch):
    with pytest.raises(ValidationError):
        patch()


@pytest.mark.parametrize("identity", [True, 1.0, "12", 2**63, -(2**63) - 1])
def test_ids_cannot_be_rounded_or_coerced(identity):
    with pytest.raises(ValidationError):
        ChatObservation(chat_id=identity, type="group")


def test_dates_must_identify_an_instant_and_archive_has_its_own_stable_id():
    with pytest.raises(ValidationError):
        ChatObservation(chat_id=1, type="private", observed_at="2026-01-02T03:04:05")
    update = ArchivedUpdate(update_id=1, kind="message", handled=True, data={})
    assert ArchivedUpdate.model_validate_json(update.model_dump_json()).id == update.id
    assert update.id != ArchivedUpdate(update_id=1, kind="message", handled=True, data={}).id
    assert update.received_at.utcoffset().total_seconds() == 0


def test_computed_names_preserve_empty_and_missing_source_values():
    base = dict(id=UUID(int=1), created=datetime.now(UTC), metadata={})
    user = UserRecord(**base, user_id=1, is_bot=False, first_name="First", last_name="")
    assert user.full_name == "First "
    assert ChatRecord(**base, chat_id=1, type="group", title="", first_name="Ignored").full_name == ""
    assert ChatRecord(**base, chat_id=1, type="group").full_name is None


def test_database_validation_does_not_echo_sensitive_values():
    marker = "private-invalid-marker"
    with pytest.raises(ValidationError) as error:
        ChatObservation(chat_id=marker, type="private")
    assert marker not in str(error.value)


def reaction_observation(**changes):
    return ReactionObservation.model_validate(
        {
            "kind": "actor",
            "chat_id": -1001,
            "message_id": 42,
            "event_at": datetime(2026, 9, 18, tzinfo=UTC),
            "user_id": 10,
            "previous_active": None if changes.get("kind") == "counts" else False,
            "reactions": [{"key": "e:👍"}],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": None},
        {"actor_chat_id": -1002},
        {"kind": "counts"},
        {"previous_active": None},
        {"previous_active": "false"},
        {"previous_active": 0},
        {"kind": "counts", "user_id": None, "previous_active": False},
    ],
)
def test_reaction_snapshot_identity_cannot_be_ambiguous(changes):
    with pytest.raises(ValidationError):
        reaction_observation(**changes)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", 2**63])
def test_reaction_counts_and_message_ids_are_strict_positive_bounded_integers(value):
    with pytest.raises(ValidationError):
        ReactionValue(key="paid", count=value)
    with pytest.raises(ValidationError):
        reaction_observation(message_id=value)


@pytest.mark.parametrize("value", ["", "👍", "e:", "c:", "x:👍", "paid:1", "e:\x00", "c:\n", "e:\ud800", "e:" + "👍" * 255])
def test_reaction_keys_reject_invalid_or_unbounded_values_without_exposing_them(value):
    with pytest.raises(ValidationError) as error:
        ReactionValue(key=value)
    assert "input_value" not in str(error.value)


def test_reaction_values_preserve_unicode_and_opaque_custom_ids():
    keys = ["e:❤", "e:❤️", "e:👨‍👩‍👧‍👦", "c:000custom-key", "paid"]
    assert [ReactionValue(key=key).key for key in keys] == keys
    assert ReactionValue(key="paid", count=2**63 - 1).count == 2**63 - 1


def test_actor_snapshot_deduplicates_selections_and_keeps_removal_tombstones():
    row = reaction_observation(reactions=[{"key": "e:👍"}, {"key": "e:👍"}, {"key": "c:007"}])
    assert [(item.key, item.count) for item in row.reactions] == [("e:👍", 1), ("c:007", 1)]
    assert reaction_observation(reactions=[]).reactions == []
    assert reaction_observation(user_id=None, actor_chat_id=-1002).actor_chat_id == -1002
    assert reaction_observation(previous_active=True, reactions=[]).previous_active is True
    with pytest.raises(ValidationError):
        reaction_observation(reactions=[{"key": "e:👍", "count": 2}])


def test_count_snapshot_deduplicates_without_adding_or_guessing_conflicting_totals():
    row = reaction_observation(kind="counts", user_id=None, reactions=[{"key": "e:👍", "count": 2}] * 2)
    assert [(item.key, item.count) for item in row.reactions] == [("e:👍", 2)]
    with pytest.raises(ValidationError):
        reaction_observation(kind="counts", user_id=None, reactions=[{"key": "e:👍", "count": 2}, {"key": "e:👍", "count": 3}])
    with pytest.raises(ValidationError):
        reaction_observation(reactions=[{"key": "e:👍"}] * 257)
    with pytest.raises(ValidationError):
        reaction_observation(event_at="2026-09-18T00:00:00")
