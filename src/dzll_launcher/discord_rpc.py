#!/usr/bin/env python3
# discord_rpc.py
#
# Minimal Discord Rich Presence wrapper for DZLL.
# Controlled by settings:
#   discord_rich_presence (bool)
#   discord_privacy_mode (bool)
#   discord_detail_level ("menus"|"ingame"|"server")

import time
from dataclasses import dataclass
from typing import Optional

try:
    from pypresence import Presence
except Exception:
    try:
        from .vendor.pypresence import Presence
    except Exception:
        Presence = None  # handled gracefully


# TODO: set your real Discord app client ID here
DZLL_DISCORD_CLIENT_ID = "1481093851316752417"


@dataclass
class PresenceState:
    in_server: bool = False
    server_name: str = ""
    server_map: str = ""
    players: str = ""   # "12/60"
    ip_port: str = ""   # "1.2.3.4:2302"


class DiscordRPC:
    def __init__(self, client_id: str = DZLL_DISCORD_CLIENT_ID):
        self.client_id = str(client_id)
        self.rpc = None
        self.enabled = False
        self.privacy = False
        self.detail_level = "menus"
        self._mode = "menus"
        self.state = PresenceState()
        self._connected = False
        self._last_payload = None
        self._start_ts = int(time.time())

    def available(self) -> bool:
        return Presence is not None and bool(self.client_id) and self.client_id != "000000000000000000"

    def apply_settings(self, settings: dict) -> None:
        self.enabled = bool(settings.get("discord_rich_presence", False))
        self.privacy = bool(settings.get("discord_privacy_mode", False))
        self.detail_level = str(settings.get("discord_detail_level", "menus") or "menus")

        if not self.enabled:
            self.clear()
            self.disconnect()
            return

        # enabled
        self.connect()
        self.update()  # refresh immediately

    def connect(self) -> None:
        if not self.available():
            return
        if self._connected:
            return
        try:
            self.rpc = Presence(self.client_id)
            self.rpc.connect()
            self._connected = True
        except Exception:
            self.rpc = None
            self._connected = False

    def disconnect(self) -> None:
        try:
            if self.rpc:
                self.rpc.close()
        except Exception:
            pass
        self.rpc = None
        self._connected = False
        self._last_payload = None

    def clear(self) -> None:
        # clear rich presence if possible
        try:
            if self.rpc and self._connected:
                self.rpc.clear()
        except Exception:
            pass
        self._last_payload = None

    def set_menu(self) -> None:
        self.state = PresenceState(in_server=False)
        self._mode = "menus"
        self.update()

    def set_installing_mods(self, *, server_name: str = "") -> None:
        self.state = PresenceState(in_server=False, server_name=server_name or "")
        self._mode = "installing"
        self.update()

    def set_joining(self, *, server_name: str = "") -> None:
        self.state = PresenceState(in_server=False, server_name=server_name or "")
        self._mode = "joining"
        self.update()

    def set_server(self, *, server_name: str, server_map: str, players: str, ip_port: str) -> None:
        self.state = PresenceState(
            in_server=True,
            server_name=server_name or "",
            server_map=server_map or "",
            players=players or "",
            ip_port=ip_port or "",
        )
        self._mode = "server"
        self.update()

    def update(self) -> None:
        if not self.enabled:
            return
        if not self.available():
            return
        if not self._connected or not self.rpc:
            self.connect()
        if not self._connected or not self.rpc:
            return

        payload = self._build_payload()
        if not payload:
            return

        # avoid spamming identical payloads
        if payload == self._last_payload:
            return

        try:
            self.rpc.update(**payload)
            self._last_payload = payload
        except Exception:
            # one retry next time
            self.disconnect()

    def _build_payload(self) -> Optional[dict]:
        lvl = self.detail_level
        st = self.state

        p = {
            "start": self._start_ts,
            "large_text": "DayZ Linux Launcher",
            "large_image": "dzll_logo",
        }

        if lvl == "menus":
            p["details"] = "Browsing servers"
            p["state"] = "Ready to join"
            return p

        if lvl == "ingame":
            mode = str(getattr(self, "_mode", "menus") or "menus").lower()
            if mode == "installing":
                p["details"] = "Installing mods"
                p["state"] = "Please wait…"
                return p
            if mode == "joining":
                p["details"] = "Joining server"
                p["state"] = "Launching DayZ…"
                return p
            p["details"] = "Playing DayZ"
            p["state"] = "In game"
            return p

        if lvl == "server":
            if not st.in_server:
                # Show transient states while preparing, but respect privacy.
                mode = str(getattr(self, "_mode", "menus") or "menus").lower()

                if mode == "installing":
                    p["details"] = "Installing Mods"
                    if self.privacy or not (st.server_name or "").strip():
                        p["state"] = "Please wait…"
                    else:
                        p["state"] = (st.server_name.strip() or "Preparing Server")[:128]
                    return p

                if mode == "joining":
                    p["details"] = "Joining server"
                    if self.privacy or not (st.server_name or "").strip():
                        p["state"] = "Launching DayZ…"
                    else:
                        p["state"] = (st.server_name.strip() or "Launching DayZ…")[:128]
                    return p

                # Default menu state
                p["details"] = "Browsing servers"
                p["state"] = "Ready to join"
                return p

            # In-server (privacy mode)
            if self.privacy:
                p["details"] = "Playing DayZ"
                p["state"] = "Server details hidden"
                return p

            # In-server (public): line 2 = Playing DayZ - <Map>, line 3 = <Server Name>
            name = (st.server_name or "").strip() or "DayZ Server"
            m = (st.server_map or "").strip()

            if m:
                p["details"] = f"Playing DayZ - {m}"[:128]
            else:
                p["details"] = "Playing DayZ"[:128]

            p["state"] = name[:128]
            return p

        p["details"] = "Browsing servers"
        p["state"] = "Ready to join"
        return p
