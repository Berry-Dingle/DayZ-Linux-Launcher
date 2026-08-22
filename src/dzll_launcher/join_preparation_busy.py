"""Authoritative read-only Join/shared-preparation busy state."""


def shared_join_preparation_busy(owner) -> bool:
    """Return whether foreground Join or background preparation owns work."""
    if getattr(getattr(owner, "_join_attempts", None), "active", None) is not None:
        return True
    gate = getattr(owner, "_preparation_operation_gate", None)
    if gate is not None and bool(getattr(gate, "active_owner", "")):
        return True
    queue = getattr(owner, "_background_prepare_queue", None)
    return bool(queue is not None and getattr(queue, "busy", False))


def shared_join_preparation_state(owner) -> str:
    """Return the reason Join is unavailable without flattening blocked into active."""

    if getattr(getattr(owner, "_join_attempts", None), "active", None) is not None:
        return "join_active"
    gate = getattr(owner, "_preparation_operation_gate", None)
    if gate is not None:
        if bool(getattr(gate, "blocked_reap_failure", False)):
            return "blocked_reap_failure"
        if bool(getattr(gate, "active_owner", "")):
            return "preparation_active"
    queue = getattr(owner, "_background_prepare_queue", None)
    if queue is not None:
        snapshot = queue.snapshot()
        if bool(getattr(snapshot, "blocked_reap_failure", False)):
            return "blocked_reap_failure"
        if bool(getattr(snapshot, "busy", False)):
            return "background_active"
    return "idle"
