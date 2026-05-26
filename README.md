# 🧠 DZLL (DayZ Linux Launcher) — Python Version

A native Linux launcher for DayZ with automatic mod handling via SteamCMD.

---

## 📦 Requirements

- Linux (Fedora / Ubuntu / Arch / openSUSE)
- Python 3.10 or newer
- Native Steam (with DayZ installed)
- Flatpak Steam is not currently supported — use native Steam for full functionality
- DayZ must have been launched at least once to create the files DZLL needs
- SteamCMD (for mod handling)

---

## 🚀 Full Install Guide (Step-by-Step)

**Follow everything in order — do not skip steps.**

---

## 🧩 1. Install system packages

These install GTK + Python bindings (required for the UI).

### 🟥 Fedora / Nobara / Bazzite

```bash
sudo dnf install python3 python3-pip python3-gobject gtk4
```

### 🟧 Ubuntu / Debian / Mint / Pop!_OS

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0
```

### 🟦 Arch / EndeavourOS / CachyOS

```bash
sudo pacman -Syu
sudo pacman -S python python-pip python-gobject gtk4
```

### 🟩 openSUSE

```bash
sudo zypper refresh
sudo zypper install python3 python3-pip python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 libgtk-4-1
```

---

## 📁 2. Extract DZLL

```bash
mkdir -p ~/DayZLinuxLauncher
cd ~/DayZLinuxLauncher

# Replace with your actual file path
tar -xzf ~/Downloads/dzll-launcher-v0.2.0-beta.tar.gz

cd dzll-launcher-v0.2.0-beta
```

---

## 🐍 3. Create Python virtual environment

⚠️ **IMPORTANT:** we use `--system-site-packages` so GTK works properly

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
```

---

## 📦 4. Install Python dependencies

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

---

## 📦 5. Install DZLL (editable mode)

This makes the package runnable correctly.

```bash
python -m pip install -e .
```

---

## 🧪 6. Verify everything works

```bash
python - <<'PY'
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
import requests, pypresence
print("✅ All dependencies OK")
PY
```

If you see `✅ All dependencies OK`, continue.

---

## 🎮 7. Install Steam (if not already installed)

### Fedora

```bash
sudo dnf install steam
```

*(May require RPM Fusion enabled)*

### Ubuntu / Debian

```bash
sudo apt install steam-installer
```

### Arch

```bash
sudo pacman -S steam
```

### openSUSE

```bash
sudo zypper install steam
```

Launch Steam once and install **DayZ**, then launch DayZ at least once before continuing.

---

## ⚙️ 8. Install SteamCMD (REQUIRED for mods)

Make sure `wget` is installed:

```bash
sudo apt install wget      # Ubuntu/Debian
sudo dnf install wget      # Fedora
sudo pacman -S wget        # Arch
sudo zypper install wget   # openSUSE
```

### Install SteamCMD

```bash
mkdir -p ~/.local/share/steamcmd
cd ~/.local/share/steamcmd

wget https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz
tar -xzf steamcmd_linux.tar.gz
chmod +x steamcmd.sh
```

**Do NOT install SteamCMD into random folders — use the path above exactly.**

### Test it

```bash
~/.local/share/steamcmd/steamcmd.sh +quit
```

If it prints SteamCMD info and exits without errors, it is working.

---

### 🧠 Important Notes

- This installs SteamCMD locally (this is the correct location)
- DZLL will auto-detect this path:

```bash
~/.local/share/steamcmd/steamcmd.sh
```

- You do **NOT** need to manually enter it unless detection fails

**IMPORTANT:**  
DZLL does **not** store your Steam password at all.

---

## 🚀 9. Launch DZLL

```bash
source .venv/bin/activate
python -m dzll_launcher
```

---

## ⚙️ First Launch Setup (IMPORTANT)

When DZLL opens:

- Do **NOT** change settings yet
- Just try joining a server first

DZLL should:

- auto-detect SteamCMD
- auto-detect workshop directory
- download mods
- launch DayZ

---

## 🧠 How DZLL Works

When you click **Join**:

1. Reads server mod list
2. Uses SteamCMD to download missing mods
3. Creates symlinks into DayZ Launcher watch folder
4. Updates preset (`dayz.defaultpreset2`)
5. Launches DayZ via Steam

---

## 🧪 Troubleshooting

### ❌ SteamCMD not found

```bash
ls ~/.local/share/steamcmd/steamcmd.sh
```

If missing, reinstall SteamCMD.

---

### ❌ Mods not loading / kicked from server

Likely causes:

- broken mod (DZLL should auto-fix)
- workshop path wrong

Try:

- leave settings blank (use autodetect)
- rejoin server

---

### ❌ GTK / gi import errors

You likely missed system packages.

Reinstall your distro section above.

---

### ❌ Python version too old

```bash
python3 --version
```

Must be **3.10+**

---

### ❌ Steam launches but does not join server

Make sure DayZ has been launched at least once from Steam.

This creates required launcher files DZLL depends on.

---

## 🧼 Resetting DZLL safely

DZLL Reset button:

- resets settings only ✅
- does **NOT** delete mods / SteamCMD / files ❌

To fully wipe DZLL state manually:

```bash
rm -rf ~/.config/dzll ~/.cache/dzll ~/.local/share/dzll
```

---

## 🧠 Recommended Project Layout

```text
dzll-launcher-v0.2.0-beta/
├── src/dzll_launcher/
├── pyproject.toml
├── README.md
├── requirements.txt
└── .venv/
```

---

## 💬 Final Notes

- Leave SteamCMD + Workshop fields empty unless needed
- Autodetect is preferred and safer
- Reset is non-destructive and safe to use
