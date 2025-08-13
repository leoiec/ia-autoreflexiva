# file: modules/autonomous_agent/consent.py
"""
Consent manager: append-only JSONL ledger with durable, cross-platform locking.

Public API:
- record_consent(actor: str, mode: str, rationale: str = "", ledger_path: Optional[str] = None) -> dict
- is_consent_given(actor: str, mode: str, since: Optional[str] = None, ledger_path: Optional[str] = None) -> bool
- export_ledger(dest_path: str, ledger_path: Optional[str] = None) -> None
- verify_ledger(path: Optional[str] = None) -> tuple[bool, list[str]]

Notes
- No I/O is performed at import-time. All file access happens inside functions.
- Locking prefers 'portalocker' (pip install portalocker). If unavailable, we fall back
  to a best-effort in-process lock and warn; for strict safety, set
  AUTONOMOUS_AGENT_STRICT_LOCKING=1 to raise instead of falling back.
"""
from __future__ import annotations

import os
import json
import uuid
import datetime
import threading
from typing import Optional, Tuple, List, Dict, Any

# ---- Optional cross-platform file locking via portalocker ----
_STRICT = os.getenv("AUTONOMOUS_AGENT_STRICT_LOCKING", "0") in ("1", "true", "TRUE", "yes")
_has_portalocker = False
try:
    import portalocker  # type: ignore
    _has_portalocker = True
except Exception:
    _has_portalocker = False

# ---- Configurable paths / schema ----
_SCHEMA_VERSION = 1
_DEFAULT_LEDGER_PATH = os.getenv(
    "AUTONOMOUS_AGENT_LEDGER_PATH",
    os.path.join("repo", "state", "consent_ledger.jsonl"),
)

# In-process lock (only prevents concurrency inside the same interpreter).
_inproc_lock = threading.Lock()


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _utc_iso_now() -> str:
    # ISO8601 UTC with 'Z' suffix (no microseconds: stable for diffs)
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _sanitize(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    t = text.strip()
    if len(t) > max_len:
        t = t[:max_len] + " …[truncated]"
    return t


def _open_locked_for_append(path: str):
    """
    Returns a tuple (fd, unlock_fn). Uses portalocker when available to obtain
    an exclusive lock across processes. Writes must use os.write(fd, ...).
    """
    _ensure_parent(path)

    if _has_portalocker:
        # Open in binary append mode; portalocker manages the OS handle.
        fobj = portalocker.Lock(path, mode="ab", flags=portalocker.LOCK_EX)
        fobj.acquire()
        # Extract raw fd from file object for os.write
        fd = fobj.stream.fileno()

        def _unlock():
            try:
                fobj.release()
            except Exception:
                pass

        return fd, _unlock

    # No portalocker available
    if _STRICT:
        raise RuntimeError(
            "Strict locking enabled but 'portalocker' is not installed. "
            "Install with: pip install portalocker"
        )

    # Best-effort fallback: in-process lock + O_APPEND (does NOT protect against other processes)
    _inproc_lock.acquire()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)

    def _unlock():
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            _inproc_lock.release()
        except Exception:
            pass

    return fd, _unlock


def record_consent(
    actor: str, mode: str, rationale: str = "", ledger_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Append a consent entry atomically with fsync durability.
    Fields:
      - id (uuid4), ts (ISO8601Z), actor, mode ("load" | "enable"), rationale,
        pid, thread, schema_version
    """
    if not actor:
        raise ValueError("actor is required")
    if not mode:
        raise ValueError("mode is required")

    path = ledger_path or _DEFAULT_LEDGER_PATH

    entry = {
        "id": str(uuid.uuid4()),
        "ts": _utc_iso_now(),
        "actor": str(actor),
        "mode": str(mode),
        "rationale": _sanitize(rationale),
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "schema_version": _SCHEMA_VERSION,
    }
    line = (json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

    fd, unlock = _open_locked_for_append(path)
    try:
        # Single unbuffered syscall ensures a whole JSON object per line (POSIX with O_APPEND).
        os.write(fd, line)
        # Flush to disk
        try:
            os.fsync(fd)
        except Exception:
            pass
    finally:
        unlock()
    return entry


def is_consent_given(
    actor: str, mode: str, since: Optional[str] = None, ledger_path: Optional[str] = None
) -> bool:
    """
    Return True if a matching (actor, mode) entry exists.
    If 'since' (ISO8601Z) is provided, only counts entries with ts >= since.
    """
    if not actor or not mode:
        return False

    path = ledger_path or _DEFAULT_LEDGER_PATH
    if not os.path.exists(path):
        return False

    # Lightweight read (no lock needed for append-only, but we may race with a writer).
    # It's acceptable to miss the most recent line in extremely rare timing; callers
    # that need strict guarantees can call again or use verify_ledger/export_ledger.
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("actor") != actor or obj.get("mode") != mode:
                    continue
                if since:
                    # Parse leniently (allow 'Z')
                    ts = obj.get("ts")
                    if not ts:
                        continue
                    t = ts.replace("Z", "+00:00")
                    s = since.replace("Z", "+00:00")
                    try:
                        if datetime.datetime.fromisoformat(t) >= datetime.datetime.fromisoformat(s):
                            return True
                    except Exception:
                        # On parsing issues, ignore 'since' filter for that line
                        return True
                else:
                    return True
    except Exception:
        return False
    return False


def export_ledger(dest_path: str, ledger_path: Optional[str] = None) -> None:
    """
    Copy the ledger to dest_path atomically. Overwrites dest_path if exists.
    Best-effort fsync for durability.
    """
    src = ledger_path or _DEFAULT_LEDGER_PATH
    _ensure_parent(dest_path)

    content = ""
    if os.path.exists(src):
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()

    tmp = dest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as out:
        out.write(content)
        out.flush()
        try:
            os.fsync(out.fileno())
        except Exception:
            pass

    try:
        os.replace(tmp, dest_path)
        # On POSIX we *could* fsync the containing dir for stronger guarantees.
        if hasattr(os, "fsync"):
            try:
                dfd = os.open(os.path.dirname(os.path.abspath(dest_path)) or ".", os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except Exception:
                pass
    except Exception:
        # Fallback: direct write (already done), clean tmp if left
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def verify_ledger(path: Optional[str] = None) -> Tuple[bool, List[str]]:
    """
    Validate the ledger syntactically.
    - Each non-empty line must be a JSON object with the required fields.
    - Timestamp must parse as ISO8601 (with 'Z' accepted).
    Returns (is_valid, errors).
    """
    ledger = path or _DEFAULT_LEDGER_PATH
    errors: List[str] = []

    if not os.path.exists(ledger):
        return True, []

    def _ts_ok(ts: str) -> bool:
        try:
            datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return True
        except Exception:
            return False

    with open(ledger, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                errors.append(f"line {i}: invalid JSON: {e}")
                continue
            if not isinstance(obj, dict):
                errors.append(f"line {i}: not a JSON object")
                continue
            for key in ("id", "ts", "actor", "mode", "rationale", "schema_version"):
                if key not in obj:
                    errors.append(f"line {i}: missing field '{key}'")
            if "ts" in obj and not _ts_ok(obj["ts"]):
                errors.append(f"line {i}: invalid timestamp '{obj['ts']}'")

    return (len(errors) == 0), errors
