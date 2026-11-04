from types import SimpleNamespace

import pytest

from dzll_launcher import server_companion_state as state
from dzll_launcher.server_endpoint import ServerEndpointValidationError
from dzll_launcher.storage import human_last_played
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import fav_key

# The persisted last-Companion-server record. Key order and value types are
# part of the on-disk shape written by storage.save_last_companion_server.
PERSISTED_KEYS = [
    "ip", "gport", "qport", "name", "map_name", "map", "mods_json", "mod_count",
    "mods_preview", "password", "third_person", "country", "time", "timewarp",
    "players", "max_players", "ping", "bm_rank",
]


def restore(data, *, objects_by_key=None, favorites=None, last_played=None):
    return state.obj_from_persisted(
        data,
        key_for=fav_key,
        objects_by_key={} if objects_by_key is None else objects_by_key,
        favorites={} if favorites is None else favorites,
        last_played={} if last_played is None else last_played,
    )


def full_server():
    return ServerObject(
        ip="1.2.3.4", gport=2302, qport=2303, name="Alpha", map_name="chernarusplus",
        mods_json="[1,2]", mod_count=2, mods_preview="CF, VPP", password=True,
        third_person=True, country="GB", time="12:30", timewarp=2.5, players=41,
        max_players=60, ping=37, bm_rank=12,
    )


def test_persisted_record_has_exact_shape_order_and_types():
    record = state.persisted_from_obj(full_server())

    assert list(record) == PERSISTED_KEYS
    assert record == {
        "ip": "1.2.3.4", "gport": 2302, "qport": 2303, "name": "Alpha",
        "map_name": "chernarusplus", "map": "chernarusplus", "mods_json": "[1,2]",
        "mod_count": 2, "mods_preview": "CF, VPP", "password": True,
        "third_person": True, "country": "GB", "time": "12:30", "timewarp": 2.5,
        "players": 41, "max_players": 60, "ping": 37, "bm_rank": 12,
    }
    assert type(record["timewarp"]) is float
    assert all(type(record[k]) is int for k in ("gport", "qport", "mod_count", "players", "max_players", "ping", "bm_rank"))


def test_persisted_record_defaults_for_missing_falsy_and_malformed_values():
    obj = SimpleNamespace(
        ip=" 5.6.7.8 ", gport=2402, qport=2403, name=None, map_name=None, mods_json=None,
        mod_count="bad", players=None, max_players="x", ping=0, bm_rank=0, timewarp=0.0,
        mods_preview=None, country=None, time=None,
    )

    record = state.persisted_from_obj(obj)

    assert list(record) == PERSISTED_KEYS
    assert record == {
        "ip": "5.6.7.8", "gport": 2402, "qport": 2403, "name": "", "map_name": "",
        "map": "", "mods_json": "", "mod_count": 0, "mods_preview": "",
        "password": False, "third_person": False, "country": "", "time": "",
        "timewarp": 1.0, "players": 0, "max_players": 0,
        # `or -1` / `or 999999999` treat a falsy 0 as missing.
        "ping": -1, "bm_rank": 999999999,
    }


@pytest.mark.parametrize("obj", [
    SimpleNamespace(ip="example.com", gport=2302, qport=2303),
    SimpleNamespace(ip="1.2.3.4", gport=2302, qport=0),
    SimpleNamespace(),
])
def test_persisted_record_rejects_invalid_endpoints(obj):
    with pytest.raises(ServerEndpointValidationError):
        state.persisted_from_obj(obj)


def test_persisted_record_round_trips_through_restore():
    record = state.persisted_from_obj(full_server())
    restored = restore(record)

    assert isinstance(restored, ServerObject)
    assert state.persisted_from_obj(restored) == record


def test_restore_returns_live_row_for_known_endpoint():
    live = full_server()
    restored = restore({"ip": "1.2.3.4", "gport": 2302, "qport": 2303, "name": "Stale"},
                       objects_by_key={fav_key("1.2.3.4", 2302): live})

    assert restored is live


@pytest.mark.parametrize("data", [
    None, [], {}, {"ip": "example.com", "gport": 2302, "qport": 2303},
    {"ip": "::1", "gport": 2302, "qport": 2303},
])
def test_restore_rejects_invalid_records(data):
    assert restore(data) is None


def test_restore_builds_detached_row_with_defaults_favourite_and_last_played():
    key = fav_key("11.0.0.1", 2302)
    joined = 1_700_000_000
    restored = restore(
        {"ip": " 11.0.0.1 ", "gport": 2302, "qport": None, "map": "enoch",
         "players": "bad", "ping": 0, "timewarp": None, "mod_count": "4", "password": 1},
        favorites={key: True},
        last_played={key: joined},
    )

    assert isinstance(restored, ServerObject)
    assert (restored.ip, restored.gport, restored.qport) == ("11.0.0.1", 2302, 2303)
    assert restored.fav is True
    assert restored.played == human_last_played(joined)
    assert restored.name == "Unknown Server"
    assert restored.map_name == "enoch"
    assert restored.time == "--:--"
    assert restored.password is True
    assert restored.third_person is False
    assert (restored.mod_count, restored.players, restored.ping, restored.bm_rank) == (4, 0, -1, 999999999)
    assert restored.timewarp == 1.0


def test_restore_prefers_map_name_over_legacy_map_key():
    restored = restore({"ip": "12.0.0.1", "gport": 2302, "qport": 2310,
                        "map_name": "deerisle", "map": "chernarusplus"})

    assert restored.map_name == "deerisle"
    assert restored.played == ""
    assert restored.fav is False


def test_snapshot_shape_and_mode_text():
    snapshot = state.snapshot_from_obj(full_server())

    assert snapshot == {
        "name": "Alpha", "map": "chernarusplus", "ping": 37, "players": 41,
        "max_players": 60, "time": "12:30", "online": True,
        "mode": "3PP / Password / Mods: 2", "ip": "1.2.3.4", "qport": 2303,
    }


def test_snapshot_offline_queue_and_malformed_values():
    obj = SimpleNamespace(ping="x", players=None, max_players="bad", mod_count=None,
                          queue="3", ip=None, qport=None, name=None, map_name=None, time=None)

    snapshot = state.snapshot_from_obj(obj)

    assert snapshot == {
        "name": "", "map": "", "ping": -1, "players": 0, "max_players": 0,
        "time": "", "online": False, "mode": "1PP / No password / Mods: 0",
        "ip": "", "qport": 0, "queue": 3,
    }
    assert "queue" not in state.snapshot_from_obj(SimpleNamespace(queue=-1))
