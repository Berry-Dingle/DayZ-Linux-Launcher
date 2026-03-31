import os
import re
from pathlib import Path

PFX_DRIVE_C = Path.home() / ".local/share/Steam/steamapps/compatdata/221100/pfx/drive_c"
BI_BASE = PFX_DRIVE_C / "users/steamuser/AppData/Local/Bohemia_Interactive_a.s"

# Confirmed launcher values:
# DoNothing, Minimize (and likely Close, but you only asked DoNothing/Minimize mapping)
_MODE_ON  = "Minimize"
_MODE_OFF = "DoNothing"

def _find_dayzlauncher_user_config() -> Path | None:
    """
    Find newest DayZ Launcher user.config (version folder changes).
    """
    if not BI_BASE.exists():
        return None
    candidates = list(BI_BASE.glob("DayZLauncher.exe_Url_*/**/user.config"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]

def set_launcher_shutdown_mode(minimize: bool) -> bool:
    """
    minimize=True  -> LauncherShutdownMode=Minimize
    minimize=False -> LauncherShutdownMode=DoNothing
    Atomic + fail-open.
    """
    cfg = _find_dayzlauncher_user_config()
    if not cfg:
        print("[DZLL] DayZ Launcher user.config not found (fail-open)")
        return False

    wanted = _MODE_ON if minimize else _MODE_OFF

    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")

        pat = (
            r'(<setting\s+name="LauncherShutdownMode"\s+serializeAs="String">\s*'
            r'<value>)(.*?)(</value>\s*</setting>)'
        )
        m = re.search(pat, text, flags=re.DOTALL)
        if not m:
            print("[DZLL] LauncherShutdownMode not found in user.config (fail-open)")
            return False

        current = (m.group(2) or "").strip()
        if current == wanted:
            return True

        out = re.sub(pat, r"\1" + wanted + r"\3", text, flags=re.DOTALL)

        tmp = cfg.with_suffix(cfg.suffix + ".tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, cfg)

        print(f"[DZLL] LauncherShutdownMode: {current!r} -> {wanted!r}")
        return True

    except Exception as e:
        print(f"[DZLL] Failed to set LauncherShutdownMode (fail-open): {e}")
        try:
            tmp = cfg.with_suffix(cfg.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False