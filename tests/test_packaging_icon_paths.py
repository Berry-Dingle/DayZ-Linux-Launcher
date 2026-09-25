from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ICON_RELATIVE_PATH = "src/dzll_launcher/images/com.bdingle.dzll.png"
ICON_NAME = "com.bdingle.dzll.png"
ICON_SIZE_DIR = "hicolor/512x512/apps"
OLD_ICON_SIZE_DIR = "hicolor/" + "256x256/apps"


def test_rpm_and_source_desktop_icon_paths_use_canonical_size():
    spec = (ROOT / "dzll_launcher.spec").read_text(encoding="utf-8")
    window = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")

    assert ICON_RELATIVE_PATH in spec
    assert f"%{{_datadir}}/icons/{ICON_SIZE_DIR}/{ICON_NAME}" in spec
    assert f"%{{_datadir}}/icons/{OLD_ICON_SIZE_DIR}/{ICON_NAME}" not in spec

    assert f".local/share/icons/{ICON_SIZE_DIR}" in window
    assert f".local/share/icons/{OLD_ICON_SIZE_DIR}" not in window


def test_debian_icon_path_remains_canonical_and_old_path_is_absent():
    debian_install = (ROOT / "debian/install").read_text(encoding="utf-8")

    assert f"{ICON_RELATIVE_PATH} usr/share/icons/{ICON_SIZE_DIR}/" in debian_install
    assert f"usr/share/icons/{OLD_ICON_SIZE_DIR}/{ICON_NAME}" not in debian_install


def test_canonical_icon_source_exists():
    assert (ROOT / ICON_RELATIVE_PATH).is_file()
