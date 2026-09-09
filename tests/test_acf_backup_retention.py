import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import steam_ugc_backend, workshop_mods
from dzll_launcher.steam_ugc_backend import UGCSubscriptionSnapshot


APPID = steam_ugc_backend.DAYZ_APPID


def _acf_with_ids(root: Path, ids) -> Path:
    path = root / "steamapps/workshop" / f"appworkshop_{APPID}.acf"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = "".join(
        f'\t\t"{int(mod_id)}"\n\t\t{{\n\t\t\t"manifest"\t"1"\n\t\t}}\n'
        for mod_id in ids
    )
    path.write_text(
        '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n'
        f"{entries}\t}}\n}}\n",
        encoding="utf-8",
    )
    return path


def _validator(root: Path):
    relative = (f"appworkshop_{APPID}.acf",)
    return lambda path: steam_ugc_backend._safe_native_workshop_path_for_root(
        root.resolve(),
        path,
        relative_parts=relative,
        leaf_kind="file",
    )


def _eligible_backups(acf: Path) -> list[Path]:
    out = []
    for entry in acf.parent.iterdir():
        if steam_ugc_backend._acf_backup_name_match(acf.name, entry.name) is None:
            continue
        info = os.lstat(entry)
        if stat.S_ISREG(info.st_mode):
            out.append(entry)
    return out


def _seed_backups(acf: Path, count: int, *, pid=700) -> list[Path]:
    out = []
    for index in range(count):
        timestamp = f"200001{index + 1:02d}-010203"
        path = acf.with_name(f"{acf.name}.bak.{timestamp}.{pid}")
        path.write_bytes(f"generation-{index}".encode("ascii"))
        ns = 1_700_000_000_000_000_000 + index
        os.utime(path, ns=(ns, ns))
        out.append(path)
    return out


def _remove(acf: Path, root: Path, mod_id: int, *, log_fn=None):
    return steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf],
        [mod_id],
        path_validator=_validator(root),
        log_fn=log_fn,
    )


@pytest.mark.parametrize(
    ("existing", "expected"),
    [(0, 1), (4, 5), (5, 5), (6, 5), (20, 5)],
)
def test_successful_rewrite_converges_to_retention_limit(
    monkeypatch, tmp_path, existing, expected,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    seeded = _seed_backups(acf, existing)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = _remove(acf, root, 101)

    assert result["ok"] is True
    backups = _eligible_backups(acf)
    assert len(backups) == expected
    if existing >= 5:
        assert not seeded[0].exists()
        assert Path(result["backups"][0]).exists()


def test_retention_keeps_newest_successive_backup_contents(monkeypatch, tmp_path):
    root = tmp_path / "library"
    ids = list(range(1001, 1010))
    acf = _acf_with_ids(root, ids)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )
    successive_contents = []

    for mod_id in ids:
        before = acf.read_bytes()
        result = _remove(acf, root, mod_id)
        assert result["ok"] is True
        assert Path(result["backups"][0]).read_bytes() == before
        successive_contents.append(before)

    retained = _eligible_backups(acf)
    assert len(retained) == 5
    assert {backup.read_bytes() for backup in retained} == set(successive_contents[-5:])


@pytest.mark.parametrize("current_timestamp", ["20000101-010203", "19700101-000000"])
def test_current_backup_survives_future_dated_history(
    monkeypatch, tmp_path, current_timestamp,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    original = acf.read_bytes()
    historical = []
    for day in range(1, 6):
        path = acf.with_name(
            f"{acf.name}.bak.209901{day:02d}-010203.70"
        )
        path.write_bytes(f"future-{day}".encode("ascii"))
        historical.append(path)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: current_timestamp,
    )

    result = _remove(acf, root, 101)

    current = Path(result["backups"][0])
    assert result["ok"] is True
    assert current.exists() and current.read_bytes() == original
    assert len(_eligible_backups(acf)) == 5
    assert not historical[0].exists()
    assert all(path.exists() for path in historical[1:])


def test_current_backup_survives_future_mtime_history(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    original = acf.read_bytes()
    historical = []
    for index, pid in enumerate(range(70, 75)):
        path = acf.with_name(
            f"{acf.name}.bak.20990101-010203.{pid}"
        )
        path.write_bytes(f"future-mtime-{index}".encode("ascii"))
        ns = 4_000_000_000_000_000_000 + index
        os.utime(path, ns=(ns, ns))
        historical.append(path)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = _remove(acf, root, 101)

    current = Path(result["backups"][0])
    assert result["ok"] is True
    assert current.exists() and current.read_bytes() == original
    assert len(_eligible_backups(acf)) == 5
    assert not historical[0].exists()
    assert all(path.exists() for path in historical[1:])


def test_replaced_current_backup_inode_is_not_treated_as_protected(
    monkeypatch, tmp_path,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    _seed_backups(acf, 5)
    replacement = b"unrelated replacement inode"
    messages = []
    real_prune = steam_ugc_backend._prune_old_acf_backups

    def replace_before_prune(*args, **kwargs):
        protected = Path(kwargs["protected_backup_path"])
        protected.unlink()
        protected.write_bytes(replacement)
        return real_prune(*args, **kwargs)

    def fail_log(_message):
        raise RuntimeError("caller logger failed")

    def fail_module_log(_message):
        raise RuntimeError("module logger failed")

    monkeypatch.setattr(
        steam_ugc_backend, "_prune_old_acf_backups", replace_before_prune,
    )
    monkeypatch.setattr(steam_ugc_backend.logger, "warning", fail_module_log)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = _remove(acf, root, 101, log_fn=fail_log)

    replaced_path = Path(result["backups"][0])
    assert result["ok"] is True
    assert replaced_path.read_bytes() == replacement
    assert len(_eligible_backups(acf)) == 6


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("", True),
        (".1", True),
        (".998", True),
        (".999", True),
        (".0", False),
        (".1000", False),
        ("." + ("9" * 1000), False),
    ],
)
def test_collision_counter_match_is_limited_to_writer_contract(suffix, expected):
    source = f"appworkshop_{APPID}.acf"
    candidate = f"{source}.bak.20990101-010203.77{suffix}"

    assert (
        steam_ugc_backend._acf_backup_name_match(source, candidate) is not None
    ) is expected


def test_writer_impossible_collision_counter_is_preserved(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    _seed_backups(acf, 6)
    impossible = acf.with_name(
        f"{acf.name}.bak.19990101-010203.77.1000"
    )
    impossible.write_bytes(b"ambiguous user file")
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = _remove(acf, root, 101)

    assert result["ok"] is True
    assert impossible.read_bytes() == b"ambiguous user file"
    assert len(_eligible_backups(acf)) == 5


def test_same_second_collision_counters_sort_numerically(tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    timestamp = "20990101-010203"
    candidates = {}
    for counter in (0, 1, 2, 9, 10, 11):
        suffix = "" if counter == 0 else f".{counter}"
        path = acf.with_name(f"{acf.name}.bak.{timestamp}.77{suffix}")
        path.write_bytes(str(counter).encode("ascii"))
        os.utime(path, ns=(1_800_000_000_000_000_000,) * 2)
        candidates[counter] = path

    steam_ugc_backend._prune_old_acf_backups(
        acf,
        validate_authoritative_path=lambda: None,
    )

    assert {int(path.read_text()) for path in _eligible_backups(acf)} == {
        1, 2, 9, 10, 11,
    }
    assert not candidates[0].exists()


def test_same_second_multiple_pids_use_mtime_not_pid(tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    timestamp = "20990101-010203"
    candidates = []
    for index, pid in enumerate((999999, 2, 30, 4, 50, 6)):
        path = acf.with_name(f"{acf.name}.bak.{timestamp}.{pid}")
        path.write_bytes(str(pid).encode("ascii"))
        ns = 1_800_000_000_000_000_000 + index
        os.utime(path, ns=(ns, ns))
        candidates.append(path)

    steam_ugc_backend._prune_old_acf_backups(
        acf,
        validate_authoritative_path=lambda: None,
    )

    assert not candidates[0].exists()
    assert all(path.exists() for path in candidates[1:])


def test_unrelated_and_special_files_are_never_pruned(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    _seed_backups(acf, 6)
    unrelated = [
        acf.with_name(f"{acf.name}.bak"),
        acf.with_name(f"{acf.name}.bak.manual"),
        acf.with_name(f"{acf.name}.copy"),
        acf.with_name(f".{acf.name}.editor.tmp"),
        acf.parent / "unrelated.txt",
    ]
    for path in unrelated:
        path.write_bytes(b"preserve")

    external = tmp_path / "outside"
    external.write_bytes(b"external")
    symlink = acf.with_name(f"{acf.name}.bak.19990101-010203.1")
    symlink.symlink_to(external)
    broken = acf.with_name(f"{acf.name}.bak.19990102-010203.1")
    broken.symlink_to(tmp_path / "missing")
    directory = acf.with_name(f"{acf.name}.bak.19990103-010203.1")
    directory.mkdir()
    fifo = acf.with_name(f"{acf.name}.bak.19990104-010203.1")
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = _remove(acf, root, 101)

    assert result["ok"] is True
    assert len(_eligible_backups(acf)) == 5
    assert all(path.exists() for path in unrelated)
    assert symlink.is_symlink() and broken.is_symlink()
    assert directory.is_dir()
    if hasattr(os, "mkfifo"):
        assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert external.read_bytes() == b"external"


def test_candidate_inode_replacement_is_not_deleted(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    oldest = _seed_backups(acf, 6)[0]
    replacement = b"replacement inode"
    real_lstat = steam_ugc_backend.os.lstat
    replaced = False

    def replace_before_revalidation(path):
        nonlocal replaced
        if Path(path) == oldest and not replaced:
            replaced = True
            oldest.unlink()
            oldest.write_bytes(replacement)
        return real_lstat(path)

    monkeypatch.setattr(steam_ugc_backend.os, "lstat", replace_before_revalidation)

    steam_ugc_backend._prune_old_acf_backups(
        acf,
        validate_authoritative_path=lambda: None,
    )

    assert replaced is True
    assert oldest.read_bytes() == replacement


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("transient")])
def test_prune_failure_is_nonfatal_and_continues(
    monkeypatch, tmp_path, error,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    seeded = _seed_backups(acf, 7)
    blocked = seeded[0]
    real_unlink = Path.unlink
    messages = []

    def fail_one(path, *args, **kwargs):
        if Path(path) == blocked:
            raise error
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = _remove(acf, root, 101, log_fn=messages.append)

    assert result["ok"] is True
    assert blocked.exists()
    assert not seeded[1].exists()
    assert Path(result["backups"][0]).exists()
    assert any(str(blocked) in message and str(error) in message for message in messages)


@pytest.mark.parametrize(
    ("caller_mode", "module_raises"),
    [
        ("works", True),
        ("raises", False),
        ("raises", True),
        ("missing", True),
    ],
)
def test_prune_diagnostic_failures_never_break_successful_rewrite(
    monkeypatch, tmp_path, caller_mode, module_raises,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    seeded = _seed_backups(acf, 7)
    blocked = seeded[0]
    real_unlink = Path.unlink
    caller_messages = []
    module_messages = []

    def fail_one(path, *args, **kwargs):
        if Path(path) == blocked:
            raise PermissionError("blocked prune")
        return real_unlink(path, *args, **kwargs)

    def caller_log(message):
        if caller_mode == "raises":
            raise RuntimeError("caller logger failed")
        caller_messages.append(message)

    def module_log(message):
        if module_raises:
            raise RuntimeError("module logger failed")
        module_messages.append(message)

    monkeypatch.setattr(Path, "unlink", fail_one)
    monkeypatch.setattr(steam_ugc_backend.logger, "warning", module_log)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )
    log_fn = None if caller_mode == "missing" else caller_log

    result = _remove(acf, root, 101, log_fn=log_fn)

    assert result["ok"] is True
    assert '"101"' not in acf.read_text(encoding="utf-8")
    assert blocked.exists()
    assert Path(result["backups"][0]).exists()
    if caller_mode == "works":
        assert caller_messages
    elif not module_raises:
        assert module_messages


def test_unexpected_retention_exception_is_nonfatal_after_commit(
    monkeypatch, tmp_path,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    original = acf.read_bytes()

    def fail_prune(*_args, **_kwargs):
        raise RuntimeError("unexpected retention failure")

    def fail_log(_message):
        raise RuntimeError("caller logger failed")

    def fail_module_log(_message):
        raise RuntimeError("module logger failed")

    monkeypatch.setattr(steam_ugc_backend, "_prune_old_acf_backups", fail_prune)
    monkeypatch.setattr(steam_ugc_backend.logger, "warning", fail_module_log)

    result = _remove(acf, root, 101, log_fn=fail_log)

    assert result["ok"] is True
    assert '"101"' not in acf.read_text(encoding="utf-8")
    assert Path(result["backups"][0]).read_bytes() == original


def test_backup_creation_failure_does_not_prune(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    original = acf.read_bytes()
    seeded = _seed_backups(acf, 6)

    def fail_backup(_fd, _payload):
        raise OSError("backup write failed")

    monkeypatch.setattr(steam_ugc_backend, "_write_all_and_fsync", fail_backup)
    result = _remove(acf, root, 101)

    assert result["ok"] is False
    assert acf.read_bytes() == original
    assert all(path.exists() for path in seeded)
    assert len(_eligible_backups(acf)) == 6
    assert not list(acf.parent.glob(f".{acf.name}.*.tmp"))


def test_atomic_replace_failure_retains_new_backup_without_pruning(
    monkeypatch, tmp_path,
):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    original = acf.read_bytes()
    seeded = _seed_backups(acf, 6)
    real_replace = steam_ugc_backend.os.replace

    def fail_replace(source, destination):
        if Path(destination) == acf:
            raise OSError("replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(steam_ugc_backend.os, "replace", fail_replace)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )
    result = _remove(acf, root, 101)

    assert result["ok"] is False
    assert acf.read_bytes() == original
    assert all(path.exists() for path in seeded)
    backups = _eligible_backups(acf)
    assert len(backups) == 7
    assert any(path.read_bytes() == original and path not in seeded for path in backups)
    assert not list(acf.parent.glob(f".{acf.name}.*.tmp"))


def test_retention_is_isolated_per_library(monkeypatch, tmp_path):
    root_a = tmp_path / "library-a"
    root_b = tmp_path / "library-b"
    acf_a = _acf_with_ids(root_a, [101])
    acf_b = _acf_with_ids(root_b, [202])
    _seed_backups(acf_a, 6, pid=701)
    backups_b = _seed_backups(acf_b, 6, pid=702)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    assert _remove(acf_a, root_a, 101)["ok"] is True
    assert len(_eligible_backups(acf_a)) == 5
    assert len(_eligible_backups(acf_b)) == 6
    assert all(path.exists() for path in backups_b)

    assert _remove(acf_b, root_b, 202)["ok"] is True
    assert len(_eligible_backups(acf_b)) == 5


def test_mod_manager_bulk_cleanup_inherits_retention(monkeypatch, tmp_path):
    root = tmp_path / "library"
    ids = list(range(1001, 1010))
    acf = _acf_with_ids(root, ids)
    monkeypatch.setattr(steam_ugc_backend, "_native_steam_roots", lambda: [root.resolve()])
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SimpleNamespace(value="offline")),
    )
    monkeypatch.setattr(steam_ugc_backend, "_mark_metadata_deleted", lambda _mid: None)
    monkeypatch.setattr(
        workshop_mods,
        "remove_dzll_symlinks_for_mod",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )
    snapshot = UGCSubscriptionSnapshot(
        valid=True,
        requested_ids=frozenset(ids),
        subscribed_ids=frozenset({999}),
        states={
            **{mid: {"id": mid, "subscribed": False, "installed": False} for mid in ids},
            999: {"id": 999, "subscribed": True, "installed": True},
        },
        logged_on=True,
        steam_id=76561198000000001,
        native_attachment_verified=True,
        created_monotonic=time.monotonic(),
    )

    results = [
        steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
            mod_id,
            steam_absence_verified=True,
            subscription_snapshot=snapshot,
        )
        for mod_id in ids
    ]

    assert all(result["ok"] for result in results)
    assert all(f'"{mod_id}"' not in acf.read_text(encoding="utf-8") for mod_id in ids)
    assert len(_eligible_backups(acf)) == 5


def test_stale_acf_scrub_inherits_retention(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, [101])
    _seed_backups(acf, 5)
    monkeypatch.setattr(steam_ugc_backend, "_native_steam_roots", lambda: [root.resolve()])
    monkeypatch.setattr(
        steam_ugc_backend, "_native_appworkshop_acf_paths", lambda _appid: [acf],
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_args, **_kwargs: (
            True,
            {101: {"id": 101, "subscribed": False, "installed": False}},
        ),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_has_real_native_workshop_folder",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    result = steam_ugc_backend.scrub_stale_dayz_workshop_acf()

    assert result["ok"] is True
    assert result["removed_ids"] == [101]
    assert len(_eligible_backups(acf)) == 5
