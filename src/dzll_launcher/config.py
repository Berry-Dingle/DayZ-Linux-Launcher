# config.py
import os

APP_ID = "com.bdingle.dzll"

# ----------------------------
# VERSION / UPDATES
# ----------------------------
APP_VERSION = "v0.1.0"
RELEASES_URL = "https://github.com/Berry-Dingle/DayZ-Linux-Launcher/releases"
GITHUB_LATEST_API = "https://api.github.com/repos/Berry-Dingle/DayZ-Linux-Launcher/releases/latest"

# ----------------------------
# WINDOW (LOCKED)
# ----------------------------
WINDOW_DEFAULT_SIZE = (1200, 708)

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

DISCLAIMER_GAP_ABOVE = 20    # gap between logo/BMC and disclaimer (px)
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

# Dead server cache (local)
CACHE_DIR = os.path.expanduser("~/.cache/dzll")
DEAD_PATH = os.path.join(CACHE_DIR, "dead.json")

# Steam global player count (official endpoint)
STEAM_CURRENT_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=221100"
# How often we poll Steam for global players (seconds)
GLOBAL_PLAYERS_POLL_SECS = 300  # 5 minutes

# Offline behavior:
# - offline recheck runs every 5 minutes
# - if a server fails 6 offline rechecks (~30 minutes), hide it until next app run
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
BATCH_SIZE = 100
MAX_WORKERS = 20
# High-priority executor for manual refresh
HI_WORKERS = 4

OFFLINE_RECHECK_SECS = 300      # 5 minutes
REFRESH_RATE_LIMIT_SECS = 1.0   # per-server

# Logo (relative path for RPM safety)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")
LOGO_PATH = os.path.join(IMAGES_DIR, "logo.png")

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