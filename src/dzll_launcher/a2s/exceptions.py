class BrokenMessageError(Exception):
    pass

class BufferExhaustedError(BrokenMessageError):
    pass


class AliveButInfoUnavailable(Exception):
    """The expected endpoint replied validly, but not with INFO before the limit."""

    def __init__(self, reason="alive-but-info-unavailable", *, remaining_deadline_ms=0.0):
        super().__init__(reason)
        self.remaining_deadline_ms = float(remaining_deadline_ms)
