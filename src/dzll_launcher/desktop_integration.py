"""Best-effort desktop entry and icon installation for source and venv runs."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def ensure_user_desktop_integration(
    *,
    app_id: str,
    app_name: str,
    icon_src_path: str,
    categories: str = "Game;",
) -> None:
    """
    Best-effort desktop integration for source/venv usage via: python -m dzll_launcher

    Behavior:
      - installs icon to ~/.local/share/icons/hicolor/512x512/apps/com.bdingle.dzll.png
      - creates ~/.local/share/applications/<app_id>.desktop
      - launches via project venv python if available
      - supports src-layout projects
      - derives paths from the actual installed package location

    SAFETY:
      - will NOT overwrite an existing desktop file unless it contains the DZLL marker
      - intended for non-packaged runs; packaged installs should ship their own .desktop/icon
    """
    try:
        home = Path.home()
        user_apps = home / ".local/share/applications"
        user_icons = home / ".local/share/icons/hicolor/512x512/apps"
        desktop_path = user_apps / f"{app_id}.desktop"
        marker = "X-DZLL-AutoCreated=true"
        system_desktop_path = Path("/usr/share/applications") / f"{app_id}.desktop"

        if system_desktop_path.exists():
            try:
                if desktop_path.exists():
                    txt = desktop_path.read_text(encoding="utf-8", errors="replace")
                    if marker in txt:
                        desktop_path.unlink()
            except Exception:
                pass
            return

        user_apps.mkdir(parents=True, exist_ok=True)
        user_icons.mkdir(parents=True, exist_ok=True)

        # ----- icon -----
        icon_name = app_id
        icon_dst = user_icons / f"{icon_name}.png"
        src = Path(icon_src_path).expanduser().resolve()

        if src.is_file():
            need_copy = not icon_dst.exists()
            if not need_copy:
                try:
                    need_copy = (
                        icon_dst.stat().st_size != src.stat().st_size
                        or int(icon_dst.stat().st_mtime) != int(src.stat().st_mtime)
                    )
                except Exception:
                    need_copy = True

            if need_copy:
                try:
                    shutil.copy2(src, icon_dst)
                except Exception as e:
                    print(f"[DZLL] Failed to copy desktop icon: {e}")

        # ----- desktop entry -----
        # Do not overwrite non-DZLL desktop entries (e.g. packaged installs)
        if desktop_path.exists():
            try:
                txt = desktop_path.read_text(encoding="utf-8", errors="replace")
                if marker not in txt:
                    return
            except Exception:
                return

        package_dir = Path(__file__).resolve().parent

        # src layout:
        #   /some/path/project/src/dzll_launcher
        # normal layout:
        #   /some/path/project/dzll_launcher
        if package_dir.parent.name == "src":
            run_path = package_dir.parent  # .../project/src
            project_root = run_path.parent  # .../project
        else:
            run_path = package_dir.parent  # .../project
            project_root = run_path

        # Prefer project-local venv python
        venv_python = project_root / ".venv" / "bin" / "python3"
        if venv_python.is_file():
            python_exe = venv_python
        else:
            python_exe = Path(sys.executable) if sys.executable else Path("/usr/bin/python3")

        exec_line = f'"{python_exe}" -m dzll_launcher'

        desktop_text = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            f"Name={app_name}\n"
            f"Comment={app_name}\n"
            f"Exec={exec_line}\n"
            f"Path={run_path}\n"
            f"Icon={icon_name}\n"
            "Terminal=false\n"
            f"Categories={categories}\n"
            "StartupNotify=true\n"
            f"{marker}\n"
        )

        try:
            old = ""
            if desktop_path.exists():
                old = desktop_path.read_text(encoding="utf-8", errors="replace")
            if old != desktop_text:
                desktop_path.write_text(desktop_text, encoding="utf-8")
                os.chmod(desktop_path, 0o755)
        except Exception as e:
            print(f"[DZLL] Failed to write desktop entry: {e}")
            return

        # Refresh desktop/icon caches best-effort
        try:
            subprocess.run(
                ["update-desktop-database", str(user_apps)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

        try:
            subprocess.run(
                ["gtk-update-icon-cache", "-f", "-t", str(home / ".local/share/icons/hicolor")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    except Exception as e:
        print(f"[DZLL] Desktop integration skipped: {e}")
