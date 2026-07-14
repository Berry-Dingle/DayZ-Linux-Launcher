# config.py
import os

APP_ID = "com.bdingle.dzll"

# ----------------------------
# VERSION / UPDATES
# ----------------------------
APP_VERSION = "v0.3.1-beta"
RELEASES_URL = "https://github.com/Berry-Dingle/DayZ-Linux-Launcher/releases"
GITHUB_LATEST_API = "https://api.github.com/repos/Berry-Dingle/DayZ-Linux-Launcher/releases/latest"

# ----------------------------
# WINDOW (LOCKED)
# ----------------------------
WINDOW_DEFAULT_SIZE = (1200, 754)

# ----------------------------
# TWEAKABLES (LOCKED LAYOUT)
# ----------------------------
RIGHT_BLOCK_WIDTH = 450
NAME_BLOCK_MIN_WIDTH = 100   # minimum width for NAME / IP / MODS block

LOGO_WIDTH_RATIO = 0.5
LOGO_MIN_HEIGHT = 60

FAV_STAR_WIDTH = 40
DIVIDER_COLOR = "#56575a"

SIDEBAR_WIDTH = 220          # HARD fixed sidebar width (px)
SIDEBAR_INNER_PADDING = 10   # padding implemented as margins inside sidebar frame (px)

DISCLAIMER_GAP_ABOVE = 4    # gap between logo/BMC and disclaimer (px)
DISCLAIMER_COLOR = "#717171"

ICON_COL_WIDTH = 18          # width for lock/3pp icon column

PING_MAX = 250  # app “max acceptable” (servers often cap ~300)

# thresholds (ms)
PING_GOOD = 60
PING_OK_GREENY = 100
PING_MED_YELLOW = 140
PING_MED_ORANGEY = 190

# Fixed right-block column widths (single source of truth)
# TIME, PLAYED, MAP, PLAYERS, PING, REFRESH, JOIN
RIGHT_COL_PX = (60, 40, 140, 70, 50, 40, 40)

# Favorites file (canonical)
CFG_DIR = os.path.expanduser("~/.config/dzll")
FAV_PATH = os.path.join(CFG_DIR, "favorites.json")

# Last played file (local)
LAST_PLAYED_PATH = os.path.join(CFG_DIR, "last_played.json")
LAST_PLAYED_PRUNE_DAYS = 90

# Last Companion server (local app state)
LAST_COMPANION_SERVER_PATH = os.path.join(CFG_DIR, "last_companion_server.json")
COMPANION_RESTART_LEARNING_PATH = os.path.join(CFG_DIR, "companion_restart_learning.json")

# Dead server cache (local)
CACHE_DIR = os.path.expanduser("~/.cache/dzll")
DEAD_PATH = os.path.join(CACHE_DIR, "dead.json")

# Steam global player count (official endpoint)
STEAM_CURRENT_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=221100"
# How often we poll Steam for global players (seconds)
GLOBAL_PLAYERS_POLL_SECS = 60

# Companion server polling
COMPANION_POLL_ONLINE_SECONDS = 10
COMPANION_POLL_OFFLINE_SECONDS = 3
COMPANION_ALERT_REARM_OFFLINE_SECONDS = 60

# Offline behavior:
# - offline recheck runs every 5 minutes
# - repeated failures keep a server marked OFFLINE; Online Only is the user-visible hide control
DEAD_MAX_FAILS = 6
DEAD_HIDE_DAYS = 0  # not used in current logic

# DB & BL Github Paths
DATA_BRANCH = "data"
REPO_RAW_BASE = "https://raw.githubusercontent.com/Berry-Dingle/DayZ-Linux-Launcher"

# DB fetch (canonical from GitHub)
DB_URL = f"{REPO_RAW_BASE}/{DATA_BRANCH}/data/dzll-servers.db"
DB_LOCAL_DIR = os.path.expanduser("~/.local/share/dzll")
DB_LOCAL_PATH = os.path.join(DB_LOCAL_DIR, "dzll-servers.db")

# Blocklist fetch
BL_URL = f"{REPO_RAW_BASE}/{DATA_BRANCH}/data/blocklist.json"
BL_LOCAL_DIR = os.path.join(os.path.expanduser("~/.local/share"), "dzll")
BL_LOCAL_PATH = os.path.join(BL_LOCAL_DIR, "blocklist.json")

# Live refresh rules
PING_CUTOFF_MS = 250
STARTUP_PING_FIRST_N = 50
STARTUP_LIVE_REST_WORKERS = 32
STARTUP_LIVE_REST_TIMEOUT_SECS = 0.9
STARTUP_LIVE_FLUSH_MAX = 75
STARTUP_LIVE_FLUSH_MS = 150
BATCH_SIZE = 100
MAX_WORKERS = 20
# High-priority executor for manual refresh
HI_WORKERS = 4

OFFLINE_RECHECK_SECS = 300      # 5 minutes
REFRESH_RATE_LIMIT_SECS = 1.0   # per-server

# Browser live-refresh: only visible rows plus a small lookahead, non-structural.
BROWSER_LIVE_INTERVAL_SECS = 5
BROWSER_LIVE_AHEAD_ROWS = 20
BROWSER_LIVE_AHEAD_PER_TICK = 8
BROWSER_LIVE_WORKERS = 8
BROWSER_LIVE_TIMEOUT_SECS = 1.25
BROWSER_LIVE_PING_DAMPEN_MS = 8
BROWSER_LIVE_FALLBACK_ROW_HEIGHT_PX = 64

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")

DISCLAIMER_TEXT = (
    "DayZ® is a registered trademark of\n"
    "Bohemia Interactive.\n"
    "DZLL is an unofficial community-made\n"
    "launcher and is not affiliated with or\n"
    "endorsed by Bohemia Interactive."
)

# Test server identification
TEST_SERVER_MARKERS = (
    "test",
    "testserver",
    "test server",
    "testing",
    "tester",
    "dev",
    "dev server",
    "devserver",
    "development",
    "development server",
    "developpement",
    "playtest",
    "dev build",
    "teste",
    "|test|",
)
