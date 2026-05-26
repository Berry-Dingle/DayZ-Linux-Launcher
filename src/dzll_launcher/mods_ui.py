# mods_ui.py
import os
import shutil
import threading
import time
from pathlib import Path
from .ui_row import attach_pointer_cursor
from gi.repository import Gtk, GLib, Pango

from .settings import autodetect_workshop_dir
from .steamcmd_mods import delete_single_mod

APPID = "221100"


def _paths(workshop_dir: str = "", proton_prefix: str = ""):
    home = Path.home()
    steamapps = home / ".local/share/Steam/steamapps"
    workshop = Path(workshop_dir).expanduser() if workshop_dir else steamapps / "workshop"
    pfx = Path(proton_prefix).expanduser() if proton_prefix else steamapps / f"compatdata/{APPID}/pfx"
    pfx_user = pfx / "drive_c/users/steamuser"
    return {
        "steamapps": steamapps,
        "workshop": workshop,
        "acf": workshop / f"appworkshop_{APPID}.acf",
        "content_app": workshop / f"content/{APPID}",
        "downloads": workshop / "downloads",
        "temp": workshop / "temp",
        "watch_1": pfx_user / "DZLLMods",
        "watch_2": pfx_user / "Documents/Templates",
        "launcher_state": pfx_user / "AppData/Local/DayZ Launcher",
        # Hard safety guard targets (MUST NOT TOUCH)
        "game_manifest": steamapps / f"appmanifest_{APPID}.acf",
        "game_dir": steamapps / "common/DayZ",
    }


def _read_installed_mod_ids(workshop_dir: str = "") -> list[int]:
    p = _paths(workshop_dir=workshop_dir)["acf"]
    if not p.is_file():
        return []
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    key = '"WorkshopItemsInstalled"'
    i = txt.find(key)
    if i < 0:
        return []
    ob = txt.find("{", i)
    if ob < 0:
        return []

    depth = 0
    end = -1
    for j in range(ob, len(txt)):
        c = txt[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end < 0:
        return []

    block = txt[ob:end + 1]
    out: list[int] = []
    seen = set()
    for line in block.splitlines():
        s = line.strip()
        if len(s) >= 3 and s[0] == '"' and s[-1] == '"' and s[1:-1].isdigit():
            mid = int(s[1:-1])
            if mid > 0 and mid not in seen:
                out.append(mid)
                seen.add(mid)
    return out


def _name_map_from_symlinks(proton_prefix: str = "") -> dict[int, str]:
    watch = _paths(proton_prefix=proton_prefix)["watch_1"]
    mp: dict[int, str] = {}
    if not watch.is_dir():
        return mp
    try:
        for nm in os.listdir(str(watch)):
            lp = watch / nm
            if not lp.is_symlink():
                continue
            try:
                tgt = Path(os.path.realpath(str(lp)))
            except Exception:
                continue
            if tgt.name.isdigit():
                mid = int(tgt.name)
                if mid > 0:
                    mp[mid] = nm
    except Exception:
        pass
    return mp


def reset_all_mods_safely(log_fn=None, *, workshop_dir: str = "", proton_prefix: str = "") -> bool:
    """
    Delete ALL DayZ workshop mods + workshop staging/state.
    HARD SAFETY: never touches appmanifest_221100.acf or steamapps/common/DayZ.
    """
    import subprocess

    def log(msg: str):
        if callable(log_fn):
            log_fn(msg)
        else:
            print(msg)

    p = _paths(workshop_dir=workshop_dir, proton_prefix=proton_prefix)
    workshop = p["workshop"].resolve()
    if (
        not workshop.is_dir()
        or workshop.name != "workshop"
        or workshop.parent.name != "steamapps"
        or not any(
            marker.exists()
            for marker in (
                workshop / f"appworkshop_{APPID}.acf",
                workshop / f"content/{APPID}",
                workshop / "downloads",
                workshop / "temp",
            )
        )
    ):
        log(f"[MOD RESET] Refusing to delete: workshop path is not a valid DayZ Steam workshop root: {workshop}")
        return False

    # Stop Steam/SteamCMD best-effort
    try:
        subprocess.run(["pkill", "-x", "steam"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", "steamcmd"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # Remove symlinks pointing into workshop/content/221100
    def wipe_symlinks(watch_dir: Path):
        if not watch_dir.is_dir():
            return
        try:
            for nm in os.listdir(str(watch_dir)):
                lp = watch_dir / nm
                if not lp.is_symlink():
                    continue
                try:
                    tgt = Path(os.path.realpath(str(lp)))
                except Exception:
                    continue
                if f"/workshop/content/{APPID}/" in str(tgt):
                    try:
                        lp.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    wipe_symlinks(p["watch_1"])
    wipe_symlinks(p["watch_2"])

    # Reset DayZ Launcher state (backup then remove Local.json + Presets)
    try:
        st = p["launcher_state"]
        if st.is_dir():
            bak = st.with_name(f"{st.name}.bak.{int(time.time())}")
            shutil.copytree(st, bak, dirs_exist_ok=True)
            try:
                (st / "Local.json").unlink()
            except Exception:
                pass
            shutil.rmtree(st / "Presets", ignore_errors=True)
    except Exception:
        pass

    # Wipe workshop ACF + dirs
    try:
        if p["acf"].exists():
            p["acf"].unlink()
    except Exception:
        log("[MOD RESET] Failed to delete appworkshop_221100.acf")
        return False

    for d in (p["content_app"], p["downloads"], p["temp"]):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    log("[MOD RESET] Completed.")
    return True


class ModsManagerOverlay:
    def __init__(self, host, overlay: Gtk.Overlay):
        self.host = host
        self.overlay = overlay

        self.scrim = Gtk.Box()
        self.scrim.set_hexpand(True)
        self.scrim.set_vexpand(True)
        self.scrim.set_visible(False)
        self.scrim.set_can_target(True)
        self.scrim.add_css_class("settings-scrim")

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card.set_halign(Gtk.Align.CENTER)
        self.card.set_valign(Gtk.Align.CENTER)
        self.card.set_visible(False)
        self.card.set_can_target(True)

        # Keep existing card layout/padding styling
        self.card.add_css_class("steamcmd-auth-card")
        # Add mods-specific styling hook (bg/border/radius)
        self.card.add_css_class("mods-card")

        self.card.set_margin_start(40)
        self.card.set_margin_end(40)
        self.card.set_margin_top(40)
        self.card.set_margin_bottom(40)
        self.card.set_size_request(760, -1)

        overlay.add_overlay(self.scrim)
        overlay.add_overlay(self.card)

        # ----------------------------
        # Confirm overlay (reuse warning-card styling)
        # ----------------------------
        self.confirm_scrim = Gtk.Box()
        self.confirm_scrim.set_hexpand(True)
        self.confirm_scrim.set_vexpand(True)
        self.confirm_scrim.set_visible(False)
        self.confirm_scrim.set_can_target(True)
        self.confirm_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.confirm_scrim)

        self.confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.confirm_box.set_halign(Gtk.Align.CENTER)
        self.confirm_box.set_valign(Gtk.Align.CENTER)
        self.confirm_box.set_visible(False)
        self.confirm_box.set_can_target(True)
        self.confirm_box.add_css_class("warning-card")
        overlay.add_overlay(self.confirm_box)

        # Title + body
        self.confirm_title = Gtk.Label(label="")
        self.confirm_title.set_xalign(0.0)
        self.confirm_title.set_wrap(True)
        self.confirm_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.confirm_title.add_css_class("steamcmd-heading")
        self.confirm_box.append(self.confirm_title)

        self.confirm_text = Gtk.Label(label="")
        self.confirm_text.set_xalign(0.0)
        self.confirm_text.set_wrap(True)
        self.confirm_text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.confirm_box.append(self.confirm_text)

        # Buttons
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        btn_row.set_halign(Gtk.Align.CENTER)

        self.confirm_ok_btn = Gtk.Button(label="OK")
        self.confirm_ok_btn.add_css_class("warning-btn")  # primary
        attach_pointer_cursor(self.confirm_ok_btn)
        btn_row.append(self.confirm_ok_btn)

        self.confirm_cancel_btn = Gtk.Button(label="Cancel")
        self.confirm_cancel_btn.add_css_class("suggested-action")  # matches your canonical styling
        self.confirm_cancel_btn.add_css_class("warning-btn")
        attach_pointer_cursor(self.confirm_cancel_btn)
        btn_row.append(self.confirm_cancel_btn)

        self.confirm_box.append(btn_row)

        # Click scrim to cancel
        confirm_scrim_click = Gtk.GestureClick.new()
        confirm_scrim_click.set_button(0)
        confirm_scrim_click.connect("pressed", lambda *_: self._confirm_hide_and_fire(False))
        self.confirm_scrim.add_controller(confirm_scrim_click)

        # Hook up buttons
        self._confirm_cb = None
        self.confirm_ok_btn.connect("clicked", lambda *_: self._confirm_hide_and_fire(True))
        self.confirm_cancel_btn.connect("clicked", lambda *_: self._confirm_hide_and_fire(False))

        self._rows_cache = []
        self._build()

    def _confirm_hide_and_fire(self, ok: bool):
        try:
            self.confirm_box.set_visible(False)
            self.confirm_scrim.set_visible(False)
        except Exception:
            pass

        cb = getattr(self, "_confirm_cb", None)
        self._confirm_cb = None
        if callable(cb):
            try:
                cb(bool(ok))
            except Exception:
                pass
        return False

    def _confirm_show(self, title: str, body: str, ok_label: str, cb):
        # store cb and set labels
        self._confirm_cb = cb
        try:
            self.confirm_title.set_text(title or "")
        except Exception:
            pass
        try:
            self.confirm_text.set_text(body or "")
        except Exception:
            pass
        try:
            self.confirm_ok_btn.set_label(ok_label or "OK")
        except Exception:
            pass

        # show
        try:
            self.confirm_scrim.set_visible(True)
            self.confirm_box.set_visible(True)
        except Exception:
            pass
        return False

    def show(self):
        self.scrim.set_visible(True)
        self.card.set_visible(True)
        try:
            self.search.grab_focus()
        except Exception:
            pass

    def hide(self):
        self.scrim.set_visible(False)
        self.card.set_visible(False)
        return False

    def _build(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_hexpand(True)

        heading = Gtk.Label(label="Manage Installed Mods")
        heading.set_xalign(0.0)
        heading.add_css_class("steamcmd-heading")
        heading.set_hexpand(True)
        header.append(heading)

        self.btn_close_x = Gtk.Button()
        self.btn_close_x.set_can_focus(False)
        self.btn_close_x.add_css_class("flat")
        self.btn_close_x.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        self.btn_close_x.set_tooltip_text("Close")
        attach_pointer_cursor(self.btn_close_x)
        self.btn_close_x.connect("clicked", lambda *_: self.hide())
        header.append(self.btn_close_x)

        self.card.append(header)

        self.card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search mods by name or ID…")
        self.search.set_hexpand(True)
        self.card.append(self.search)

        sc = Gtk.ScrolledWindow()
        sc.add_css_class("mods-list")
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_min_content_height(320)
        self.card.append(sc)

        self.lb = Gtk.ListBox()
        self.lb.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.set_child(self.lb)

        # Bottom buttons
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_halign(Gtk.Align.END)

        self.btn_delete_all = Gtk.Button(label="Delete All Mods")
        self.btn_delete_all.add_css_class("destructive-action")
        attach_pointer_cursor(self.btn_delete_all)  # (6)
        btn_row.append(self.btn_delete_all)

        self.card.append(btn_row)

        self.lb.set_filter_func(self._filter_row)
        self.search.connect("search-changed", lambda *_: self.lb.invalidate_filter())

        self.btn_delete_all.connect("clicked", self._on_delete_all_clicked)

        # Click scrim to close
        scrim_click = Gtk.GestureClick.new()
        scrim_click.set_button(0)  # any button
        scrim_click.connect("pressed", lambda *_: self.hide())
        self.scrim.add_controller(scrim_click)

        self.refresh()

    def _workshop_dir(self) -> str:
        try:
            win = getattr(self.host, "_win", None)
            val = str(win.settings.get("workshop_dir") or "").strip() if win else ""
            return val or autodetect_workshop_dir() or ""
        except Exception:
            return ""

    def _proton_prefix(self) -> str:
        try:
            win = getattr(self.host, "_win", None)
            if win and hasattr(win, "_get_dayz_proton_prefix"):
                return str(win._get_dayz_proton_prefix() or "").strip()
        except Exception:
            pass
        return ""

    def _clear_list(self):
        child = self.lb.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lb.remove(child)
            child = nxt

    def refresh(self):
        self._rows_cache.clear()
        self._clear_list()

        workshop_dir = self._workshop_dir()
        proton_prefix = self._proton_prefix()
        ids = _read_installed_mod_ids(workshop_dir=workshop_dir)
        name_map = _name_map_from_symlinks(proton_prefix=proton_prefix)

        items = []
        for mid in ids:
            nm_raw = (name_map.get(mid) or "").strip()

            # If missing/unknown OR looks like "@12345678" (same as ID), normalize display to "@Mod-ID"
            if (not nm_raw) or (nm_raw == f"@{mid}") or (nm_raw == str(mid)):
                nm = "@Mod-ID"
            else:
                nm = nm_raw

            items.append((nm, mid))
        items.sort(key=lambda t: (t[0].lower(), t[1]))

        for nm, mid in items:
            row = self._make_row(nm, mid)
            self.lb.append(row)
            self._rows_cache.append((row, nm.lower(), str(mid)))

        self.lb.invalidate_filter()

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        q = (self.search.get_text() or "").strip().lower()
        if not q:
            return True
        for r, nm_lc, mid_s in self._rows_cache:
            if r is row:
                return (q in nm_lc) or (q in mid_s)
        return True

    def _make_row(self, mod_name: str, mod_id: int) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        line.set_hexpand(True)

        lbl = Gtk.Label(label=f"{mod_name} - {mod_id}")
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(lbl)

        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("mod-del-btn")  # (4) styling hook for red icon
        btn.set_tooltip_text("Delete this mod")
        btn.set_child(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        btn.set_margin_end(10)           # (5) spacing from scrollbar
        attach_pointer_cursor(btn)       # (4) pointer hover
        line.append(btn)

        outer.append(line)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        row.set_child(outer)

        btn.connect("clicked", lambda *_: self._confirm_and_delete_one(mod_name, mod_id))
        return row

    def _confirm(self, title: str, body: str, cb, ok_label: str = "OK"):
        """
        Confirm using the shared warning-card overlay (NOT Gtk.AlertDialog).
        Calls cb(True/False).
        """
        GLib.idle_add(self._confirm_show, title, body, ok_label, cb)

    def _confirm_and_delete_one(self, mod_name: str, mod_id: int):
        def after(ok: bool):
            if not ok:
                return

            def worker():
                ok2 = False
                try:
                    ok2 = bool(delete_single_mod(
                        int(mod_id),
                        workshop_dir=self._workshop_dir(),
                        proton_prefix=self._proton_prefix(),
                    ))
                except Exception:
                    ok2 = False

                def done():
                    if ok2:
                        self.refresh()
                    return False

                GLib.idle_add(done)

            threading.Thread(target=worker, daemon=True).start()

        self._confirm(
            "Delete Workshop Mod?",
            f"{mod_name} ({mod_id})\n\nThis removes the mod from disk and workshop state.",
            after,
            ok_label="Delete",
        )

    def _on_delete_all_clicked(self, *_):
        def after(ok: bool):
            if not ok:
                return

            def worker():
                ok2 = reset_all_mods_safely(
                    workshop_dir=self._workshop_dir(),
                    proton_prefix=self._proton_prefix(),
                )

                def done():
                    if ok2:
                        self.refresh()
                    return False

                GLib.idle_add(done)

            threading.Thread(target=worker, daemon=True).start()

        self._confirm(
            "Delete ALL Workshop Mods?",
            "This will delete ALL downloaded DayZ Workshop mods and\n"
            "reset the Workshop state.\n"
            "The DayZ game install will NOT be touched.\n\nContinue?",
            after,
            ok_label="Delete All",
        )
