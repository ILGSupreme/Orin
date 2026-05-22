import time
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def utcnow_iso() -> str:
    return utcnow().isoformat()

def monotonic() -> float:
    return time.monotonic()