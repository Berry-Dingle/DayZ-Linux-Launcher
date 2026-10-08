"""Install the pytest profile boundary before collecting application imports.

A fixture is too late: test modules import dzll_launcher during collection, and
config.py resolves several HOME paths at import time.
"""

import atexit
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


if any(name == "dzll_launcher" or name.startswith("dzll_launcher.") for name in sys.modules):
    raise RuntimeError("DZLL was imported before the pytest profile boundary")

_original_homes = {
    Path(home).expanduser().resolve()
    for home in (os.environ.get("HOME"), pwd.getpwuid(os.getuid()).pw_dir)
    if home
}
_test_home = Path(tempfile.mkdtemp(prefix="dzll-pytest-home-"))
atexit.register(shutil.rmtree, _test_home, ignore_errors=True)
os.environ.update({
    "HOME": str(_test_home),
    "XDG_CONFIG_HOME": str(_test_home / ".config"),
    "XDG_DATA_HOME": str(_test_home / ".local" / "share"),
    "XDG_STATE_HOME": str(_test_home / ".local" / "state"),
    "XDG_CACHE_HOME": str(_test_home / ".cache"),
})

_protected_roots = tuple(
    root
    for home in _original_homes
    if home != _test_home
    for root in (
        home / ".config" / "dzll",
        home / ".local" / "share" / "dzll",
        home / ".local" / "state" / "dzll",
        home / ".cache" / "dzll",
    )
)
_violations = []


def _path(value):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    value = os.fsdecode(value)
    if value.startswith("file:"):
        parsed = urlsplit(value)
        value = unquote(parsed.path)
    return Path(os.path.realpath(value))


def _deny(event, value):
    path = _path(value)
    if path is None:
        return
    for root in _protected_roots:
        if path == root or root in path.parents:
            message = f"pytest blocked real DZLL profile access ({event}): {path}"
            _violations.append(message)
            raise RuntimeError(message)


def _audit(event, args):
    if event == "open":
        # Block reads too: the suite must never depend on private live state.
        _deny(event, args[0])
    elif event in {
        "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link",
        "os.symlink", "os.chmod", "os.chown", "os.utime", "os.truncate",
    }:
        _deny(event, args[0])
        if event in {"os.rename", "os.link", "os.symlink"}:
            _deny(event, args[1])
    elif event == "sqlite3.connect":
        _deny(event, args[0])


sys.addaudithook(_audit)


def pytest_sessionfinish(session, exitstatus):
    # Some production paths catch broad exceptions; a blocked attempt still
    # makes the entire test invocation fail.
    if _violations:
        session.exitstatus = 1
        for message in _violations:
            print(message, file=sys.stderr)
