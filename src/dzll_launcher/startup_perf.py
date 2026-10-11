"""Opt-in, low-volume phase timings for startup baseline measurements."""

import threading
import time


def log_startup_phase(origin_ns: int, phase: str, start_ns: int, *, duration_ns=None, **fields) -> None:
    """Emit one phase line; diagnostics must never affect startup recovery."""
    try:
        now_ns = time.monotonic_ns()
        origin_ns = int(origin_ns or start_ns)
        if duration_ns is None:
            duration_ns = now_ns - start_ns
        parts = [
            "[STARTUP-PERF]",
            f"phase={phase}",
            f"duration_ms={int(duration_ns) / 1_000_000:.3f}",
            f"startup_ms={(now_ns - origin_ns) / 1_000_000:.3f}",
            f"thread={threading.current_thread().name}",
        ]
        parts.extend(f"{key}={value}" for key, value in fields.items())
        print(" ".join(parts), flush=True)
    except Exception:
        pass
