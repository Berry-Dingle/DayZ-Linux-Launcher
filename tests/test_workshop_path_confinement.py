from pathlib import Path
from types import SimpleNamespace
import os
import time

import pytest

from dzll_launcher import steam_ugc_backend, steamcmd_mods
from dzll_launcher.steam_ugc_backend import UGCSubscriptionSnapshot


APPID = steam_ugc_backend.DAYZ_APPID
MOD_ID = 101


def _item(root: Path, *, branch="content", mod_id=MOD_ID) -> Path:
    path = root / "steamapps/workshop" / branch / str(APPID) / str(mod_id)
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(b"synthetic")
    return path


def _patch_native_roots(monkeypatch, *roots: Path) -> None:
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_steam_roots",
        lambda: [Path(root).resolve() for root in roots],
    )


def _content_validator(root: Path):
    relative = ("content", str(APPID), str(MOD_ID))
    return lambda path: steam_ugc_backend._safe_native_workshop_path_for_root(
        root.resolve(),
        path,
        relative_parts=relative,
        leaf_kind="directory",
    )


def _authorized_snapshot() -> UGCSubscriptionSnapshot:
    return UGCSubscriptionSnapshot(
        valid=True,
        requested_ids=frozenset({MOD_ID, 999}),
        subscribed_ids=frozenset({999}),
        states={
            MOD_ID: {"id": MOD_ID, "subscribed": False, "installed": True},
            999: {"id": 999, "subscribed": True, "installed": True},
        },
        logged_on=True,
        steam_id=76561198000000001,
        native_attachment_verified=True,
        created_monotonic=time.monotonic(),
    )


def _acf_with_ids(root: Path, *ids: int) -> Path:
    acf = root / "steamapps/workshop" / f"appworkshop_{APPID}.acf"
    acf.parent.mkdir(parents=True, exist_ok=True)
    entries = "".join(f'\t\t"{mid}"\n\t\t{{}}\n' for mid in ids)
    acf.write_text(
        '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n'
        f"{entries}\t}}\n}}\n",
        encoding="utf-8",
    )
    return acf


def _acf_validator(path):
    return steam_ugc_backend._safe_workshop_acf_path(path, appid=APPID)


def _patch_authorized_local_cleanup(monkeypatch, root: Path) -> None:
    _patch_native_roots(monkeypatch, root)
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SimpleNamespace(value="offline")),
    )
    monkeypatch.setattr(steam_ugc_backend, "_mark_metadata_deleted", lambda _mid: None)
    monkeypatch.setattr(
        steamcmd_mods,
        "remove_dzll_symlinks_for_mod",
        lambda *_args, **_kwargs: [],
    )


def test_native_real_content_directory_is_accepted_and_deleted(monkeypatch, tmp_path):
    root = tmp_path / "library"
    item = _item(root)
    _patch_native_roots(monkeypatch, root)

    safe = steam_ugc_backend._safe_workshop_content_path(
        str(item), mod_id=MOD_ID, appid=APPID,
    )

    assert safe == item
    assert steam_ugc_backend._delete_dir_if_present(
        safe, path_validator=_content_validator(root),
    ) is True
    assert not item.exists()


def test_symlinked_trusted_library_root_remains_supported(monkeypatch, tmp_path):
    physical = tmp_path / "Spiele-äöü" / "ゲーム"
    item = _item(physical)
    alias = tmp_path / "library-alias"
    alias.symlink_to(physical, target_is_directory=True)
    _patch_native_roots(monkeypatch, alias)

    # Production canonicalizes the trusted VDF library root before deriving
    # destructive candidates beneath it.
    safe = steam_ugc_backend._safe_workshop_content_path(
        str(item), mod_id=MOD_ID, appid=APPID,
    )

    assert safe == item
    assert steam_ugc_backend._delete_dir_if_present(
        safe, path_validator=_content_validator(alias),
    ) is True
    assert not item.exists()


def test_cross_library_redirect_cannot_change_candidate_root(monkeypatch, tmp_path):
    root_a = tmp_path / "library-a"
    root_b = tmp_path / "library-b"
    item_a = _item(root_a, mod_id=202)
    item_b = _item(root_b)
    redirected = root_a / "steamapps/workshop/content"
    original_a_content = root_a / "original-content"
    redirected.rename(original_a_content)
    redirected.symlink_to(
        root_b / "steamapps/workshop/content", target_is_directory=True,
    )
    _patch_native_roots(monkeypatch, root_a, root_b)
    candidate_a = redirected / str(APPID) / str(MOD_ID)

    assert steam_ugc_backend._safe_native_workshop_path_for_root(
        root_a.resolve(),
        candidate_a,
        relative_parts=("content", str(APPID), str(MOD_ID)),
        leaf_kind="directory",
    ) is None
    assert steam_ugc_backend._safe_native_workshop_path_for_root(
        root_b.resolve(),
        candidate_a,
        relative_parts=("content", str(APPID), str(MOD_ID)),
        leaf_kind="directory",
    ) is None
    assert steam_ugc_backend._safe_workshop_content_path(
        str(candidate_a), mod_id=MOD_ID, appid=APPID,
    ) is None
    assert steam_ugc_backend._safe_workshop_content_path(
        str(item_b), mod_id=MOD_ID, appid=APPID,
    ) == item_b
    with pytest.raises(RuntimeError, match="unsafe install folder"):
        steam_ugc_backend._native_workshop_cleanup_plan(MOD_ID, APPID)
    assert item_b.is_dir()
    assert (original_a_content / str(APPID) / "202").is_dir()


def test_cleanup_plan_keeps_every_path_with_its_constructing_root(
        monkeypatch, tmp_path):
    roots = (tmp_path / "library-a", tmp_path / "library-b")
    for root in roots:
        _item(root)
    _patch_native_roots(monkeypatch, *roots)

    plans = steam_ugc_backend._native_workshop_cleanup_plan(MOD_ID, APPID)

    assert [plan["root"] for plan in plans] == [root.resolve() for root in roots]
    for plan, root in zip(plans, roots):
        canonical = root.resolve()
        assert plan["content"] == canonical / f"steamapps/workshop/content/{APPID}/{MOD_ID}"
        assert plan["downloads"] == canonical / f"steamapps/workshop/downloads/{APPID}/{MOD_ID}"
        assert plan["downloads_patch"] == canonical / f"steamapps/workshop/downloads/state_{APPID}_{APPID}_{MOD_ID}.patch"
        assert plan["root_patch"] == canonical / f"steamapps/workshop/state_{APPID}_{APPID}_{MOD_ID}.patch"
        assert plan["acf"] == canonical / f"steamapps/workshop/appworkshop_{APPID}.acf"


@pytest.mark.parametrize("component", ["steamapps", "workshop", "content", "appid"])
@pytest.mark.parametrize("relative_link", [False, True])
def test_native_intermediate_symlink_is_rejected(
        monkeypatch, tmp_path, component, relative_link):
    root = tmp_path / "library"
    external = tmp_path / f"external-{component}-{relative_link}"
    external.mkdir()

    if component == "steamapps":
        link = root / "steamapps"
        target = external
        item = external / "workshop/content" / str(APPID) / str(MOD_ID)
    elif component == "workshop":
        link = root / "steamapps/workshop"
        target = external
        item = external / "content" / str(APPID) / str(MOD_ID)
    elif component == "content":
        link = root / "steamapps/workshop/content"
        target = external
        item = external / str(APPID) / str(MOD_ID)
    else:
        link = root / "steamapps/workshop/content" / str(APPID)
        target = external
        item = external / str(MOD_ID)

    link.parent.mkdir(parents=True)
    link_target = os.path.relpath(target, link.parent) if relative_link else target
    link.symlink_to(link_target, target_is_directory=True)
    item.mkdir(parents=True)
    (item / "outside.bin").write_bytes(b"preserve")
    _patch_native_roots(monkeypatch, root)
    candidate = root / "steamapps/workshop/content" / str(APPID) / str(MOD_ID)

    assert steam_ugc_backend._safe_workshop_content_path(
        str(candidate), mod_id=MOD_ID, appid=APPID,
    ) is None
    with pytest.raises(RuntimeError, match="unsafe Workshop"):
        steam_ugc_backend._delete_dir_if_present(
            candidate, path_validator=_content_validator(root),
        )
    assert item.is_dir()
    assert (item / "outside.bin").read_bytes() == b"preserve"


def test_native_leaf_symlink_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "library"
    external = tmp_path / "external-item"
    external.mkdir()
    (external / "outside.bin").write_bytes(b"preserve")
    leaf = root / "steamapps/workshop/content" / str(APPID) / str(MOD_ID)
    leaf.parent.mkdir(parents=True)
    leaf.symlink_to(external, target_is_directory=True)
    _patch_native_roots(monkeypatch, root)

    assert steam_ugc_backend._safe_workshop_content_path(
        str(leaf), mod_id=MOD_ID, appid=APPID,
    ) is None
    assert external.is_dir()


def test_broken_and_chained_descendant_symlinks_fail_closed(monkeypatch, tmp_path):
    root = tmp_path / "library"
    workshop = root / "steamapps/workshop"
    workshop.parent.mkdir(parents=True)
    workshop.symlink_to(tmp_path / "missing", target_is_directory=True)
    _patch_native_roots(monkeypatch, root)
    candidate = workshop / "content" / str(APPID) / str(MOD_ID)
    assert steam_ugc_backend._safe_workshop_content_path(
        str(candidate), mod_id=MOD_ID, appid=APPID,
    ) is None

    workshop.unlink()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second, target_is_directory=True)
    workshop.symlink_to(first, target_is_directory=True)
    item = second / "content" / str(APPID) / str(MOD_ID)
    item.mkdir(parents=True)
    assert steam_ugc_backend._safe_workshop_content_path(
        str(candidate), mod_id=MOD_ID, appid=APPID,
    ) is None
    assert item.is_dir()


def test_inside_library_redirect_outside_workshop_subtree_is_rejected(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    relocated = root / "relocated-content"
    item = relocated / str(APPID) / str(MOD_ID)
    item.mkdir(parents=True)
    content = root / "steamapps/workshop/content"
    content.parent.mkdir(parents=True)
    content.symlink_to(relocated, target_is_directory=True)
    _patch_native_roots(monkeypatch, root)

    candidate = content / str(APPID) / str(MOD_ID)
    assert steam_ugc_backend._safe_workshop_content_path(
        str(candidate), mod_id=MOD_ID, appid=APPID,
    ) is None
    assert item.is_dir()


def test_download_patch_and_acf_descendant_redirection_is_rejected(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    external = tmp_path / "external-workshop"
    external.mkdir()
    workshop = root / "steamapps/workshop"
    workshop.parent.mkdir(parents=True)
    workshop.symlink_to(external, target_is_directory=True)

    download = external / "downloads" / str(APPID) / str(MOD_ID)
    download.mkdir(parents=True)
    patch = external / "downloads" / f"state_{APPID}_{APPID}_{MOD_ID}.patch"
    patch.write_bytes(b"preserve patch")
    root_patch = external / f"state_{APPID}_{APPID}_{MOD_ID}.patch"
    root_patch.write_bytes(b"preserve root patch")
    acf = external / f"appworkshop_{APPID}.acf"
    acf.write_text(f'"WorkshopItemsInstalled"\n{{\n\t"{MOD_ID}"\n\t{{}}\n}}\n')
    _patch_native_roots(monkeypatch, root)

    logical = root / "steamapps/workshop"
    assert steam_ugc_backend._safe_workshop_download_dir(
        logical / "downloads" / str(APPID) / str(MOD_ID),
        mod_id=MOD_ID,
        appid=APPID,
    ) is None
    assert steam_ugc_backend._safe_workshop_patch_file(
        logical / "downloads" / patch.name, mod_id=MOD_ID, appid=APPID,
    ) is None
    assert steam_ugc_backend._safe_workshop_patch_file(
        logical / root_patch.name, mod_id=MOD_ID, appid=APPID,
    ) is None
    with pytest.raises(RuntimeError, match="unsafe Workshop ACF"):
        steam_ugc_backend._native_appworkshop_acf_paths(APPID)
    assert download.is_dir()
    assert patch.read_bytes() == b"preserve patch"
    assert root_patch.read_bytes() == b"preserve root patch"
    assert str(MOD_ID) in acf.read_text()


def test_acf_mutation_sink_revalidates_descendant_path(monkeypatch, tmp_path):
    root = tmp_path / "library"
    external = tmp_path / "external-workshop"
    external.mkdir()
    workshop = root / "steamapps/workshop"
    workshop.parent.mkdir(parents=True)
    workshop.symlink_to(external, target_is_directory=True)
    acf = external / f"appworkshop_{APPID}.acf"
    original = (
        '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n'
        f'\t\t"{MOD_ID}"\n\t\t{{}}\n\t}}\n}}\n'
    )
    acf.write_text(original, encoding="utf-8")
    _patch_native_roots(monkeypatch, root)
    logical_acf = workshop / acf.name

    result = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [logical_acf],
        [MOD_ID],
        path_validator=lambda path: steam_ugc_backend._safe_workshop_acf_path(
            path, appid=APPID,
        ),
    )

    assert result["ok"] is False
    assert "unsafe Workshop ACF" in result["error"]
    assert acf.read_text(encoding="utf-8") == original
    assert list(external.glob("*.bak.*")) == []


def test_acf_preexisting_predictable_temp_symlink_is_not_followed(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, MOD_ID)
    original = acf.read_bytes()
    external = tmp_path / "outside-temp"
    external.write_bytes(b"preserve")
    fixed = "20990101-010203"
    injected = acf.with_name(
        f"{acf.name}.tmp.{fixed}.{os.getpid()}"
    )
    injected.symlink_to(external)
    _patch_native_roots(monkeypatch, root)
    monkeypatch.setattr(steam_ugc_backend.time, "strftime", lambda *_args: fixed)

    result = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [MOD_ID], path_validator=_acf_validator,
    )

    assert result["ok"] is True
    assert external.read_bytes() == b"preserve"
    assert injected.is_symlink()
    assert acf.is_file() and not acf.is_symlink()
    assert f'"{MOD_ID}"' not in acf.read_text(encoding="utf-8")
    assert result["backups"]
    assert Path(result["backups"][0]).read_bytes() == original
    assert list(acf.parent.glob(f".{acf.name}.*.tmp")) == []


def test_acf_preexisting_backup_symlink_fails_without_following_it(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, MOD_ID)
    original = acf.read_bytes()
    external = tmp_path / "outside-backup"
    external.write_bytes(b"preserve")
    fixed = "20990101-010203"
    injected = acf.with_name(
        f"{acf.name}.bak.{fixed}.{os.getpid()}"
    )
    injected.symlink_to(external)
    _patch_native_roots(monkeypatch, root)
    monkeypatch.setattr(steam_ugc_backend.time, "strftime", lambda *_args: fixed)

    result = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [MOD_ID], path_validator=_acf_validator,
    )

    assert result["ok"] is False
    assert "symlinked Workshop ACF backup" in result["error"]
    assert acf.read_bytes() == original
    assert acf.is_file() and not acf.is_symlink()
    assert external.read_bytes() == b"preserve"
    assert injected.is_symlink()
    assert list(acf.parent.glob(f".{acf.name}.*.tmp")) == []


def test_acf_temp_and_backup_symlinks_never_modify_external_targets(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, MOD_ID)
    original = acf.read_bytes()
    external_temp = tmp_path / "outside-temp"
    external_backup = tmp_path / "outside-backup"
    external_temp.write_bytes(b"temp preserve")
    external_backup.write_bytes(b"backup preserve")
    fixed = "20990101-010203"
    temp_link = acf.with_name(f"{acf.name}.tmp.{fixed}.{os.getpid()}")
    backup_link = acf.with_name(f"{acf.name}.bak.{fixed}.{os.getpid()}")
    temp_link.symlink_to(external_temp)
    backup_link.symlink_to(external_backup)
    _patch_native_roots(monkeypatch, root)
    monkeypatch.setattr(steam_ugc_backend.time, "strftime", lambda *_args: fixed)

    result = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [MOD_ID], path_validator=_acf_validator,
    )

    assert result["ok"] is False
    assert acf.read_bytes() == original
    assert acf.is_file() and not acf.is_symlink()
    assert external_temp.read_bytes() == b"temp preserve"
    assert external_backup.read_bytes() == b"backup preserve"
    assert temp_link.is_symlink() and backup_link.is_symlink()


def test_symlinked_authoritative_acf_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "library"
    external = tmp_path / "outside.acf"
    external.write_bytes(b"preserve")
    acf = root / "steamapps/workshop" / f"appworkshop_{APPID}.acf"
    acf.parent.mkdir(parents=True)
    acf.symlink_to(external)
    _patch_native_roots(monkeypatch, root)

    result = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [MOD_ID], path_validator=_acf_validator,
    )

    assert result["ok"] is False
    assert external.read_bytes() == b"preserve"
    assert acf.is_symlink()


def test_repeated_acf_mutation_uses_distinct_safe_backups(monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, MOD_ID, 202)
    first_original = acf.read_bytes()
    _patch_native_roots(monkeypatch, root)
    monkeypatch.setattr(
        steam_ugc_backend.time, "strftime", lambda *_args: "20990101-010203",
    )

    first = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [MOD_ID], path_validator=_acf_validator,
    )
    second_original = acf.read_bytes()
    second = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [202], path_validator=_acf_validator,
    )

    assert first["ok"] is True and second["ok"] is True
    first_backup = Path(first["backups"][0])
    second_backup = Path(second["backups"][0])
    assert first_backup != second_backup
    assert first_backup.read_bytes() == first_original
    assert second_backup.read_bytes() == second_original
    assert acf.is_file() and not acf.is_symlink()
    assert '"101"' not in acf.read_text(encoding="utf-8")
    assert '"202"' not in acf.read_text(encoding="utf-8")
    assert list(acf.parent.glob(f".{acf.name}.*.tmp")) == []


def test_acf_replace_failure_preserves_authoritative_file_and_cleans_temp(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    acf = _acf_with_ids(root, MOD_ID)
    original = acf.read_bytes()
    _patch_native_roots(monkeypatch, root)
    real_replace = steam_ugc_backend.os.replace

    def fail_acf_replace(source, destination):
        if Path(destination) == acf:
            raise OSError("synthetic replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(steam_ugc_backend.os, "replace", fail_acf_replace)

    result = steam_ugc_backend._remove_workshop_acf_entries_from_paths(
        [acf], [MOD_ID], path_validator=_acf_validator,
    )

    assert result["ok"] is False
    assert "synthetic replace failure" in result["error"]
    assert acf.read_bytes() == original
    assert acf.is_file() and not acf.is_symlink()
    assert list(acf.parent.glob(f".{acf.name}.*.tmp")) == []
    backups = list(acf.parent.glob(f"{acf.name}.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_cleanup_plan_rejects_downloads_redirect_before_content_mutation(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    content = _item(root)
    external = tmp_path / "external-downloads"
    external.mkdir()
    downloads = root / "steamapps/workshop/downloads"
    downloads.symlink_to(external, target_is_directory=True)
    external_item = external / str(APPID) / str(MOD_ID)
    external_item.mkdir(parents=True)
    marker = external_item / "outside.bin"
    marker.write_bytes(b"preserve")
    _patch_authorized_local_cleanup(monkeypatch, root)

    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        MOD_ID,
        steam_absence_verified=True,
        subscription_snapshot=_authorized_snapshot(),
    )

    assert result["ok"] is False
    assert "unsafe Workshop path" in result["error"]
    assert content.is_dir()
    assert marker.read_bytes() == b"preserve"


@pytest.mark.parametrize("kind", ["download_leaf", "patch_leaf", "acf_leaf"])
def test_native_destructive_sibling_leaf_symlinks_are_rejected(
        monkeypatch, tmp_path, kind):
    root = tmp_path / "library"
    workshop = root / "steamapps/workshop"
    external = tmp_path / f"external-{kind}"
    external.mkdir()
    _patch_native_roots(monkeypatch, root)

    if kind == "download_leaf":
        candidate = workshop / "downloads" / str(APPID) / str(MOD_ID)
        candidate.parent.mkdir(parents=True)
        candidate.symlink_to(external, target_is_directory=True)
        safe = steam_ugc_backend._safe_workshop_download_dir(
            candidate, mod_id=MOD_ID, appid=APPID,
        )
    elif kind == "patch_leaf":
        candidate = workshop / "downloads" / f"state_{APPID}_{APPID}_{MOD_ID}.patch"
        candidate.parent.mkdir(parents=True)
        target = external / "outside.patch"
        target.write_bytes(b"preserve")
        candidate.symlink_to(target)
        safe = steam_ugc_backend._safe_workshop_patch_file(
            candidate, mod_id=MOD_ID, appid=APPID,
        )
    else:
        candidate = workshop / f"appworkshop_{APPID}.acf"
        candidate.parent.mkdir(parents=True)
        target = external / "outside.acf"
        target.write_bytes(b"preserve")
        candidate.symlink_to(target)
        safe = steam_ugc_backend._safe_workshop_acf_path(candidate, appid=APPID)

    assert safe is None
    assert external.is_dir()


def test_mod_manager_authorized_cleanup_deletes_normal_tree(monkeypatch, tmp_path):
    root = tmp_path / "library"
    content = _item(root)
    download = _item(root, branch="downloads")
    patch = root / "steamapps/workshop/downloads" / f"state_{APPID}_{APPID}_{MOD_ID}.patch"
    patch.write_bytes(b"staging")
    root_patch = root / "steamapps/workshop" / f"state_{APPID}_{APPID}_{MOD_ID}.patch"
    root_patch.write_bytes(b"root staging")
    acf = root / "steamapps/workshop" / f"appworkshop_{APPID}.acf"
    acf.write_text(
        '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n'
        f'\t\t"{MOD_ID}"\n\t\t{{}}\n\t}}\n}}\n',
        encoding="utf-8",
    )
    _patch_authorized_local_cleanup(monkeypatch, root)

    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        MOD_ID,
        steam_absence_verified=True,
        subscription_snapshot=_authorized_snapshot(),
    )

    assert result["ok"] is True
    assert not content.exists()
    assert not download.exists()
    assert not patch.exists()
    assert not root_patch.exists()
    assert f'"{MOD_ID}"' not in acf.read_text(encoding="utf-8")
    assert len(list(acf.parent.glob(f"{acf.name}.bak.*"))) == 1


def test_mod_manager_authority_cannot_bypass_descendant_confinement(
        monkeypatch, tmp_path):
    root = tmp_path / "library"
    external = tmp_path / "external-workshop"
    external.mkdir()
    workshop = root / "steamapps/workshop"
    workshop.parent.mkdir(parents=True)
    workshop.symlink_to(external, target_is_directory=True)
    content = external / "content" / str(APPID) / str(MOD_ID)
    content.mkdir(parents=True)
    (content / "outside.bin").write_bytes(b"preserve")
    download = external / "downloads" / str(APPID) / str(MOD_ID)
    download.mkdir(parents=True)
    patch = external / "downloads" / f"state_{APPID}_{APPID}_{MOD_ID}.patch"
    patch.write_bytes(b"preserve")
    _patch_authorized_local_cleanup(monkeypatch, root)

    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        MOD_ID,
        steam_absence_verified=True,
        subscription_snapshot=_authorized_snapshot(),
    )

    assert result["ok"] is False
    assert "unsafe install folder" in result["error"]
    assert content.is_dir()
    assert download.is_dir()
    assert patch.read_bytes() == b"preserve"


def _disable_steamcmd_process_calls(monkeypatch):
    monkeypatch.setattr(
        steamcmd_mods.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )


def test_steamcmd_normal_cleanup_and_configured_root_symlink(monkeypatch, tmp_path):
    physical_workshop = tmp_path / "SteamCMD-ゲーム" / "workshop"
    content = physical_workshop / "content" / str(APPID) / str(MOD_ID)
    content.mkdir(parents=True)
    (content / "payload.bin").write_bytes(b"synthetic")
    alias = tmp_path / "workshop-alias"
    alias.symlink_to(physical_workshop, target_is_directory=True)
    _disable_steamcmd_process_calls(monkeypatch)

    assert steamcmd_mods.delete_single_mod(
        MOD_ID, workshop_dir=str(alias), proton_prefix=str(tmp_path / "pfx"),
    ) is True
    assert not content.exists()


@pytest.mark.parametrize("component", ["content", "downloads", "leaf"])
def test_steamcmd_rejects_descendant_symlinks(monkeypatch, tmp_path, component):
    workshop = tmp_path / "workshop"
    external = tmp_path / f"external-{component}"
    external.mkdir()
    if component == "content":
        link = workshop / "content"
        item = external / str(APPID) / str(MOD_ID)
    elif component == "downloads":
        (workshop / "content" / str(APPID) / str(MOD_ID)).mkdir(parents=True)
        link = workshop / "downloads"
        item = external / str(APPID) / str(MOD_ID)
    else:
        link = workshop / "content" / str(APPID) / str(MOD_ID)
        item = external
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(external, target_is_directory=True)
    item.mkdir(parents=True, exist_ok=True)
    marker = item / "outside.bin"
    marker.write_bytes(b"preserve")
    _disable_steamcmd_process_calls(monkeypatch)

    assert steamcmd_mods.delete_single_mod(
        MOD_ID,
        workshop_dir=str(workshop),
        proton_prefix=str(tmp_path / "pfx"),
    ) is False
    assert marker.read_bytes() == b"preserve"


def test_sink_revalidates_after_descendant_is_swapped(monkeypatch, tmp_path):
    root = tmp_path / "library"
    content = root / "steamapps/workshop/content"
    original_item = _item(root)
    _patch_native_roots(monkeypatch, root)
    safe = steam_ugc_backend._safe_workshop_content_path(
        str(original_item), mod_id=MOD_ID, appid=APPID,
    )
    assert safe == original_item

    original_content = root / "original-content"
    content.rename(original_content)
    external = tmp_path / "external-content"
    external_item = external / str(APPID) / str(MOD_ID)
    external_item.mkdir(parents=True)
    marker = external_item / "outside.bin"
    marker.write_bytes(b"preserve")
    content.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unsafe Workshop"):
        steam_ugc_backend._delete_dir_if_present(
            safe, path_validator=_content_validator(root),
        )
    assert marker.read_bytes() == b"preserve"
    assert (original_content / str(APPID) / str(MOD_ID)).is_dir()


def test_multiple_unicode_libraries_are_validated_independently(monkeypatch, tmp_path):
    root_a = tmp_path / "Spiele-äöü"
    root_b = tmp_path / "ゲーム"
    item_a = _item(root_a)
    item_b = _item(root_b)
    _patch_native_roots(monkeypatch, root_a, root_b)

    assert steam_ugc_backend._safe_workshop_content_path(
        str(item_a), mod_id=MOD_ID, appid=APPID,
    ) == item_a
    assert steam_ugc_backend._safe_workshop_content_path(
        str(item_b), mod_id=MOD_ID, appid=APPID,
    ) == item_b


def test_normal_join_watch_symlink_creation_and_reuse_is_unchanged(tmp_path):
    workshop = tmp_path / "workshop"
    item = workshop / "content" / str(APPID) / str(MOD_ID)
    addons = item / "addons"
    addons.mkdir(parents=True)
    (addons / "synthetic.pbo").write_bytes(b"pbo")
    watch = tmp_path / "watch"

    created = steamcmd_mods.ensure_watch_symlinks(
        workshop_dir=str(workshop),
        mods=[(MOD_ID, "Synthetic Mod")],
        watch_folder=str(watch),
    )
    reused = steamcmd_mods.ensure_watch_symlinks(
        workshop_dir=str(workshop),
        mods=[(MOD_ID, "Synthetic Mod")],
        watch_folder=str(watch),
    )

    assert len(created["created"]) == 1
    link = Path(created["created"][0])
    assert link.is_symlink()
    assert link.resolve() == item.resolve()
    assert reused["kept"] == [str(link)]
