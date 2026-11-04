"""Server Companion alert playback and desktop notification mechanisms.

These functions only perform the side effect. The window decides whether an
alert is permitted and when it fires.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from gi.repository import Gio

SERVER_COMPANION_ALERT_SOUNDS = {
    "online": {
        "female": "OnlineF.mp3",
        "male": "OnlineM.mp3",
        "beep": "serverOnlineBeep.mp3",
    },
    "restart_warning": {
        "female": "RestartF.mp3",
        "male": "RestartM.mp3",
        "beep": "serverOnlineBeep.mp3",
    },
}

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio")
BACK_ONLINE_NOTIFICATION_ID = "dzll-server-companion-back-online"


def play_alert_sound(
    event: str,
    *,
    volume,
    sound,
    debug: Callable[[str], None],
) -> tuple[bool, str | None]:
    """Launch the alert sound for ``event`` without waiting for it.

    Returns ``(ok, message)``. A zero volume counts as a successful silent
    alert. ``message`` explains a failure for the Companion panel.
    """
    try:
        volume = max(0, min(100, int(volume)))
    except Exception:
        volume = 80
    if volume <= 0:
        debug(f"audio skipped: event={event!r} volume={volume}")
        return True, None
    sound = str(sound or "female")
    event_sounds = SERVER_COMPANION_ALERT_SOUNDS.get(str(event or "online"), SERVER_COMPANION_ALERT_SOUNDS["online"])
    audio_name = event_sounds.get(sound, event_sounds["female"])
    audio_path = os.path.join(AUDIO_DIR, audio_name)
    debug(
        f"audio selected: event={str(event or 'online')!r} sound={sound!r} "
        f"filename={audio_name!r} path={audio_path!r} exists={os.path.exists(audio_path)}"
    )
    if not os.path.exists(audio_path):
        audio_path = os.path.join(AUDIO_DIR, event_sounds["female"])
        debug(
            f"audio fallback selected: filename={event_sounds['female']!r} "
            f"path={audio_path!r} exists={os.path.exists(audio_path)}"
        )
    if not os.path.exists(audio_path):
        debug("audio unavailable: selected/fallback file missing")
        return False, "Alert audio file missing"
    player = shutil.which("ffplay")
    cmd = None
    if player:
        cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", f"volume={volume / 100:.2f}", audio_path]
    else:
        player = shutil.which("mpg123")
        if player:
            cmd = [player, "-g", str(volume), audio_path]
    if not cmd:
        debug("audio unavailable: no ffplay or mpg123 found")
        return False, "Alert audio unavailable: install FFmpeg / ffplay"
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        debug(f"audio process launched: player={cmd[0]!r} event={str(event or 'online')!r}")
        return True, None
    except Exception as exc:
        debug(f"audio process launch failed: {exc!r}")
        return False, "Alert audio unavailable: install FFmpeg / ffplay"


def send_back_online_notification(get_application: Callable[[], object], snapshot) -> None:
    """Send the "Server back online" desktop notification. Never raises."""
    try:
        app = get_application()
        if app is None:
            return
        notification = Gio.Notification.new("Server back online")
        name = str((snapshot or {}).get("name") or "").strip()
        if name:
            notification.set_body(name)
        app.send_notification(BACK_ONLINE_NOTIFICATION_ID, notification)
    except Exception:
        pass
