"""Ordered membership evidence and authenticated, bounded roster reads on real SQL."""

from datetime import UTC, datetime, timedelta
from itertools import permutations
from uuid import UUID

import pytest

from msu_hub_bot.storage.models import ChatMemberPage
from test_postgres_storage import PRINCIPAL, STRANGER, literal

NOW = datetime(2026, 9, 20, tzinfo=UTC)


def at(second):
    return (NOW + timedelta(seconds=second)).isoformat()


def member(user_id=101, *, second=10, status="member", source="chat_member", event_id=1, **changes):
    return {
        "chat_id": -101,
        "user_id": user_id,
        "observed_at": at(second),
        "observation_source": "membership",
        "status": status,
        "status_observed_at": at(second),
        "status_source": source,
        "status_event_id": event_id,
        "permissions": {},
        **changes,
    }


def batch(*members, update_id=1):
    return {
        "update_id": update_id,
        "received_at": at(100),
        "users": [
            {"user_id": row["user_id"], "is_bot": row["user_id"] == 999, "first_name": "Synthetic", "observed_at": at(100)}
            for row in members
        ],
        "chats": [{"chat_id": -101, "type": "supergroup", "observed_at": at(100)}],
        "memberships": list(members),
    }


def observe(db, *members, update_id=1):
    db.rpc("observe_memberships", literal(batch(*members, update_id=update_id)))


def page(db, *, state=None, cursor=None, limit=100, chat_id=-101):
    state_sql = "NULL" if state is None else "'" + state + "'"
    result = db.rpc("list_chat_members", f"{chat_id},{state_sql},{cursor if cursor is not None else 'NULL'},{limit}")
    return ChatMemberPage.model_validate(result)


def stored(db, user_id=101):
    return db.value(f"SELECT to_jsonb(c) FROM msu_hub_private.chat_users c WHERE chat_id=-101 AND user_id={user_id};")


def exercise_membership_migration(db, migration):
    db.run("""
        INSERT INTO msu_hub_private.users(user_id,is_bot,first_name) VALUES(18001,false,'Synthetic legacy');
        INSERT INTO msu_hub_private.chats(chat_id,type) VALUES(-18001,'supergroup');
        INSERT INTO msu_hub_private.chat_users(chat_id,user_id,first_seen_at,last_seen_at,status,permissions)
        VALUES(-18001,18001,'2020-01-01','2026-01-01','restricted','{"is_member":false,"can_send_messages":false}');
    """)

    def snapshot():
        return db.value("""
            SELECT jsonb_build_object(
                'members',(SELECT jsonb_agg(to_jsonb(c) ORDER BY chat_id,user_id) FROM msu_hub_private.chat_users c),
                'columns',(SELECT jsonb_agg(jsonb_build_array(attnum,attname,atttypid) ORDER BY attnum)
                    FROM pg_attribute WHERE attrelid='msu_hub_private.chat_users'::regclass AND attnum>0),
                'functions',(SELECT jsonb_agg(jsonb_build_array(oid,prosrc,proowner,proacl,proconfig) ORDER BY oid)
                    FROM pg_proc WHERE pronamespace IN ('msu_hub_private'::regnamespace,'msu_hub_api'::regnamespace)),
                'ledger',(SELECT jsonb_agg(version ORDER BY version) FROM msu_hub_private.schema_migrations));
        """)

    before = snapshot()
    retained = db.run("SELECT pg_get_functiondef('msu_hub_private.retain_messages(integer,timestamptz)'::regprocedure);").stdout
    db.run("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(99);")
    rejected = db.run(migration, check=False)
    assert rejected.returncode and "requires schema revision 8" in rejected.stderr
    db.run("DELETE FROM msu_hub_private.schema_migrations WHERE version=99;")
    assert db.run(migration.replace("VALUES(9);", "VALUES(1/0);"), check=False).returncode
    assert snapshot() == before
    db.run(migration)
    after = snapshot()
    added = {"status_observed_at", "status_source", "status_event_id", "observation_source", "admin_lost_at"}
    assert [{key: value for key, value in row.items() if key not in added} for row in after["members"]] == before["members"]
    assert all(row[key] is None for row in after["members"] for key in added)
    assert db.run("SELECT pg_get_functiondef('msu_hub_private.retain_messages(integer,timestamptz)'::regprocedure);").stdout == retained
    assert db.value("SELECT max(version) FROM msu_hub_private.schema_migrations;") == 9
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999, "application_documents": 1, "memberships": 1}
    legacy = page(db, chat_id=-18001).members[0]
    assert legacy.status == "restricted" and legacy.is_member is False and legacy.state == "unknown"
    return True


def test_membership_upgrade_preserves_evidence_and_rolls_back_atomically(application_postgres):
    assert application_postgres.membership_upgrade


def test_activity_cannot_block_delayed_status_or_clear_permissions(application_db):
    db = application_db
    observe(db, member(status="administrator", permissions={"can_delete_messages": True}))
    observe(db, {"chat_id": -101, "user_id": 101, "observed_at": at(30), "observation_source": "message", "permissions": {}})
    assert stored(db)["status"] == "administrator"
    observe(db, member(second=20, status="member", event_id=2))
    row = page(db).members[0]
    assert row.status == "member" and row.status_observed_at == NOW + timedelta(seconds=20)
    assert row.last_seen_at == NOW + timedelta(seconds=30) and row.observation_source == "message"
    assert stored(db)["permissions"] == {}


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_same_second_strong_status_and_update_id_win_in_every_order(application_db, order):
    events = [
        member(status="administrator", event_id=100),
        member(status="left", event_id=101),
        member(source="service_join", event_id=99999),
    ]
    for index in order:
        observe(application_db, events[index])
    row = page(application_db).members[0]
    assert (row.status, row.status_source, row.status_event_id, row.state) == ("left", "chat_member", 101, "absent")


@pytest.mark.parametrize(
    "status,permissions,expected",
    [
        ("member", {}, "present"),
        ("creator", {}, "present"),
        ("administrator", {}, "present"),
        ("left", {}, "absent"),
        ("kicked", {}, "absent"),
        ("restricted", {"is_member": True}, "present"),
        ("restricted", {"is_member": False}, "absent"),
        ("restricted", {}, "unknown"),
        ("restricted", {"is_member": "false"}, "unknown"),
    ],
)
def test_presence_uses_explicit_status_and_strict_restricted_is_member(application_db, status, permissions, expected):
    observe(application_db, member(status=status, permissions=permissions))
    assert page(application_db).members[0].state == expected


def test_candidates_and_service_snapshots_do_not_retain_stale_admin_rights(application_db):
    db = application_db
    observe(db, {"chat_id": -101, "user_id": 101, "observed_at": at(1), "observation_source": "join_request"})
    assert page(db).members[0].state == "unknown"
    observe(db, member(status="administrator", permissions={"can_delete_messages": True}))
    observe(db, member(second=20, source="service_leave", status="left", event_id=88))
    row = page(db).members[0]
    assert row.state == "absent" and stored(db)["permissions"] == {}


def test_membership_only_replay_does_not_consume_archive_receipt_or_expire(application_db):
    db = application_db
    value = batch(member(status="administrator", event_id=50), update_id=50)
    db.rpc("observe_memberships", literal(value))
    snapshot = stored(db)
    db.rpc("observe_memberships", literal(value))
    assert stored(db) == snapshot
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 0
    observe(db, member(second=20, status="left", event_id=51), update_id=51)
    archive = {**value, "id": str(UUID(int=50)), "kind": "chat_member", "handled": False, "data": {}, "topics": [], "messages": []}
    db.rpc("archive_update", literal(archive))
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 1
    assert stored(db)["status"] == "left"
    db.run("SELECT msu_hub_private.retain_messages(100,'2030-01-01');")
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 0
    db.rpc("archive_update", literal(archive))
    assert stored(db)["status"] == "left" and db.value("SELECT count(*) FROM msu_hub_private.chat_users;") == 1


def test_legacy_archive_writer_remains_accepted_without_forging_profile_activity_as_status(application_db):
    db = application_db
    legacy = {"chat_id": -101, "user_id": 101, "observed_at": at(10), "status": "member"}
    value = {**batch(legacy), "id": str(UUID(int=1)), "kind": "message", "handled": True, "data": {}}
    db.rpc("archive_update", literal(value))
    row = page(db).members[0]
    assert row.state == "present" and row.status_source == "legacy"
    value.update(id=str(UUID(int=2)), update_id=2, memberships=[{"chat_id": -101, "user_id": 101, "observed_at": at(30)}])
    db.rpc("archive_update", literal(value))
    assert stored(db)["status"] == "member"
    assert page(db).members[0].status_observed_at == NOW + timedelta(seconds=10)


def test_latest_bot_availability_preserves_older_admin_loss_after_regain(application_db):
    db = application_db
    observe(db, member(999, second=30, status="administrator", source="my_chat_member", event_id=3))
    observe(db, member(999, second=20, status="left", source="my_chat_member", event_id=2, admin_lost_at=at(20)))
    coverage = page(db).coverage
    assert coverage.bot_is_admin is True and coverage.bot_state == "present" and coverage.complete is False
    assert coverage.admin_lost_at == NOW + timedelta(seconds=20)
    observe(db, member(999, second=40, status="member", source="my_chat_member", event_id=4, admin_lost_at=at(40)))
    assert page(db).coverage.bot_is_admin is False
    observe(db, member(999, second=50, status="kicked", source="my_chat_member", event_id=5))
    assert page(db).coverage.bot_state == "absent"
    assert page(db).coverage.admin_lost_at == NOW + timedelta(seconds=40)


def test_roster_is_chat_scoped_filtered_ordered_bounded_and_explicitly_incomplete(application_db):
    db = application_db
    observe(
        db,
        member(105),
        member(101),
        member(103, status="left"),
        member(102, status="restricted", permissions={"is_member": False}),
        member(104),
    )
    first = page(db, state="present", limit=2)
    assert [row.user_id for row in first.members] == [101, 104] and first.next_after_user_id == 104
    second = page(db, state="present", limit=2, cursor=first.next_after_user_id)
    assert [row.user_id for row in second.members] == [105] and second.next_after_user_id is None
    assert [row.user_id for row in page(db, state="absent").members] == [102, 103]
    assert page(db, state="unknown").members == []
    assert page(db, chat_id=-102).members == []
    assert first.coverage.complete is False and first.coverage.bot_state == "unknown" and first.coverage.bot_is_admin is None
    assert (first.coverage.observed_count, first.coverage.present_count, first.coverage.absent_count, first.coverage.unknown_count) == (
        5,
        3,
        2,
        0,
    )
    assert db.rpc("list_chat_members", "-101")["state"] == "present"


@pytest.mark.parametrize("arguments", ["0", "NULL", "-101,'invented'", "-101,NULL,NULL,0", "-101,NULL,NULL,101", "-101,NULL,NULL,NULL"])
def test_roster_rejects_invalid_query_bounds(application_db, arguments):
    result = application_db.run(f"SELECT msu_hub_api.list_chat_members_v1({arguments});", principal=PRINCIPAL, check=False)
    assert result.returncode and "Invalid membership query" in result.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"memberships": None},
        {"memberships": [{}] * 513},
        {"data": {"text": "synthetic-private"}},
        {"bot_id": 123},
        {"update_id": None},
        {"update_id": "1"},
        {"users": [{"user_id": 1, "is_bot": False, "first_name": "x" * 1048576}]},
    ],
)
def test_batch_rejects_unbounded_or_unreviewed_payload_atomically(application_db, change):
    value = {**batch(member()), **change}
    result = application_db.run(f"SELECT msu_hub_api.observe_memberships_v1({literal(value)});", principal=PRINCIPAL, check=False)
    assert result.returncode
    assert application_db.value("SELECT count(*) FROM msu_hub_private.chat_users;") == 0


@pytest.mark.parametrize(
    "change",
    [
        {"status_event_id": None},
        {"status_observed_at": None},
        {"status": None},
        {"status": "invented"},
        {"status_source": "invented"},
        {"status_source": None},
        {"status_source": "my_chat_member"},
        {"admin_lost_at": at(10)},
        {"observation_source": "invented"},
        {"permissions": []},
    ],
)
def test_inconsistent_status_evidence_is_rejected_atomically(application_db, change):
    value = batch(member(**change))
    result = application_db.run(f"SELECT msu_hub_api.observe_memberships_v1({literal(value)});", principal=PRINCIPAL, check=False)
    assert result.returncode
    assert application_db.value("SELECT count(*) FROM msu_hub_private.users;") == 0


def test_new_rpc_grants_require_enabled_principal_and_keep_tables_private(application_db):
    db = application_db
    for statement in ("SELECT msu_hub_api.list_chat_members_v1(-101);", f"SELECT msu_hub_api.observe_memberships_v1({literal(batch())});"):
        assert db.run(statement, principal=STRANGER, check=False).returncode
        assert db.run("SET ROLE anon; " + statement, check=False).returncode
    assert db.run("SELECT * FROM msu_hub_private.chat_users;", principal=PRINCIPAL, check=False).returncode
    assert db.run("SELECT msu_hub_private.membership_state('member','{}',now());", principal=PRINCIPAL, check=False).returncode
    db.run("UPDATE msu_hub_private.principals SET enabled=false;")
    assert db.run("SELECT msu_hub_api.list_chat_members_v1(-101);", principal=PRINCIPAL, check=False).returncode
    functions = db.value("""
        SELECT jsonb_agg(jsonb_build_object('owner',pg_get_userbyid(proowner),'definer',prosecdef,'config',proconfig))
        FROM pg_proc WHERE pronamespace='msu_hub_api'::regnamespace AND proname IN ('list_chat_members_v1','observe_memberships_v1');
    """)
    assert len(functions) == 2
    assert all(row == {"owner": "msu_hub_owner", "definer": True, "config": ['search_path=""']} for row in functions)
