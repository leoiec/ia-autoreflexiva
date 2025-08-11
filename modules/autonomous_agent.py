# file: modules/autonomous_agent.py
"""
Single-file, import-friendly AutonomousAgent module with explicit-consent loader semantics.

Public API:
- __version__
- make_agent(...)
- run(request_json: str) -> str
- Loader API: load_core(consent=False), enable_core(), is_consent_given(), is_core_initialized(), is_core_enabled() [alias]
- Classes: AutonomousAgent, Policy, MemoryAdapter, Decision, PatchProposal

Design goals:
- No network/credential/telemetry initialization at import-time.
- Explicit-consent loader that gates any potential heavy/IO initialization.
- Honors AGENT_LOAD_CONSENT environment variable (consent) but does not auto-initialize.
- Deterministic, testable behavior; privacy-aware memory redaction.
- Thread-safe, idempotent loader.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple
import json
import re
import time

import uuid
import io
import stat
import sys

try:
    import fcntl  # Unix
    _HAS_FCNTL = True
except Exception:
    _HAS_FCNTL = False

try:
    import msvcrt  # Windows
    _HAS_MSVCRT = True
except Exception:
    _HAS_MSVCRT = False

def _set_private_perms(path: str) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except Exception:
        pass

def _lock_fd(fd: int) -> None:
    if _HAS_FCNTL:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            pass
    elif _HAS_MSVCRT:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        except Exception:
            pass

def _unlock_fd(fd: int) -> None:
    if _HAS_FCNTL:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
    elif _HAS_MSVCRT:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass

def record_consent_attempt(actor_id: str, mode: str, reason: Optional[str] = None) -> bool:
    """
    Append one JSON object as a single atomic write.
    Adds a unique 'id' field. Best-effort durability (fsync).
    """
    path = get_ledger_path()
    entry = {
        "id": uuid.uuid4().hex,
        "timestamp_iso": datetime.datetime.utcnow().isoformat() + "Z",
        "actor_id": actor_id or "",
        "mode": mode,
        "reason": (reason or ""),
        "env_consent_flag": _env_allows_consent(),
        "pid": os.getpid(),
        "module_version": __version__,
    }
    line_bytes = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")

    _ensure_ledger_parent(path)
    fd = None
    try:
        # Open for append, write-only; create if missing with 0600 perms.
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        _lock_fd(fd)
        # Single sys-call append -> atomic on POSIX
        os.write(fd, line_bytes)
        try:
            os.fsync(fd)
        except Exception:
            pass
        _set_private_perms(path)
        return True
    except Exception:
        return False
    finally:
        try:
            if fd is not None:
                _unlock_fd(fd)
                os.close(fd)
        except Exception:
            pass

def read_ledger(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Read entries without creating the file or opening for write.
    """
    path = get_ledger_path()
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            if limit is None:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
            else:
                # Efficient tail read for limit>0
                lines = fh.read().splitlines()
                for ln in lines[-limit:]:
                    if not ln.strip():
                        continue
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
    except Exception:
        pass
    return out

def export_ledger(dest_path: str, ledger_path: Optional[str] = None) -> None:
    """
    Atomic export via temp file + replace; fsyncs file and (POSIX) parent dir.
    """
    src = ledger_path or get_ledger_path()
    content = ""
    if os.path.exists(src):
        try:
            with open(src, "r", encoding="utf-8", newline="") as fh:
                content = fh.read()
        except Exception:
            content = ""

    _ensure_ledger_parent(dest_path)
    tmp = dest_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fd:
            fd.write(content)
            fd.flush()
            try:
                os.fsync(fd.fileno())
            except Exception:
                pass
        os.replace(tmp, dest_path)
        # POSIX: fsync parent dir so rename is durable
        try:
            if os.name == "posix":
                dirfd = os.open(os.path.dirname(dest_path) or ".", os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
        except Exception:
            pass
        _set_private_perms(dest_path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def verify_ledger(path: Optional[str] = None) -> Tuple[bool, List[str]]:
    ledger = path or get_ledger_path()
    if not os.path.exists(ledger):
        return True, []
    errors: List[str] = []
    try:
        with open(ledger, "r", encoding="utf-8", newline="") as fh:
            for i, ln in enumerate(fh, 1):
                s = ln.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception as e:
                    errors.append(f"line {i}: invalid JSON: {e}")
                    continue
                for k in ("id", "timestamp_iso", "actor_id", "mode"):
                    if k not in obj:
                        errors.append(f"line {i}: missing field '{k}'")
                ts = obj.get("timestamp_iso", "")
                try:
                    # strict-ish ISO8601 with 'Z' accepted
                    datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    errors.append(f"line {i}: bad timestamp '{ts}'")
    except Exception as e:
        errors.append(f"read error: {e}")
    return (len(errors) == 0), errors



__version__ = "0.2.2"  # bump when behavior changes

# ----------------------------- Loader / Consent ------------------------- #
# This module intentionally avoids doing heavy work at import time.
# The loader API below allows an outer system or operator to explicitly
# enable any deferred initialization that might involve network, credentials,
# telemetry, or other side-effects.

_AGENT_CONSENT_ENV = "AGENT_LOAD_CONSENT"

_loader_lock = threading.Lock()
_consent_given: bool = False          # whether the operator/user has signaled consent
_core_initialized: bool = False       # whether we actually performed initialization (if any)


def _env_allows_consent() -> bool:
    v = os.getenv(_AGENT_CONSENT_ENV, "")
    return str(v).lower() in ("1", "true", "yes")


# Record environment-provided consent, but DO NOT initialize automatically.
if _env_allows_consent():
    _consent_given = True


def load_core(consent: bool = False) -> bool:
    """
    Activate the module's 'core' behavior that may perform heavier initialization
    in future (e.g., telemetry, credential loading, network clients).

    Behavior:
      - If AGENT_LOAD_CONSENT env var is set to a truthy value, consent is considered given.
      - If consent==True (or env consent), the loader may initialize.
      - Thread-safe and idempotent; only first successful init will run.

    Returns True if core is initialized after the call, False otherwise.
    """
    global _consent_given, _core_initialized
    if _env_allows_consent():
        consent = True
        _consent_given = True

    if not consent:
        return False

    if _core_initialized:
        return True

    with _loader_lock:
        if _core_initialized:
            return True
        # ---- Deferred init goes here (keep minimal, explicit, and safe). ----
        # e.g.,
        #   import some_heavy_module
        #   some_heavy_module.init(...)
        # For now we just mark initialized to keep semantics future-proof.
        _core_initialized = True
    return True


def enable_core(actor_id: Optional[str] = None, reason: Optional[str] = None) -> bool:
    """Convenience to enable core with explicit consent. Records ledger entry and then enables."""
    # Compute once so both ledger entries (enable + load) use identical metadata.
    actor = actor_id or _default_actor_id()
    why = reason or "enable_call"

    try:
        record_consent_attempt(actor, "enable", why)
    except Exception:
        pass

    # IMPORTANT: pass the same actor/reason through to load_core to avoid mismatched entries.
    return load_core(consent=True, actor_id=actor, reason=why)


def is_consent_given() -> bool:
    """Whether consent was given (via env or explicit call)."""
    return _consent_given or _env_allows_consent()


def is_core_initialized() -> bool:
    """Whether the (deferred) core initialization actually ran."""
    return _core_initialized


def is_core_enabled() -> bool:
    """Backward-compatible alias; returns initialized state (not just consent)."""
    return is_core_initialized()


# ----------------------------- Data Models ---------------------------- #

@dataclass
class Decision:
    """Outcome of a plan evaluation."""
    action: str           # "proceed" | "revise" | "reject"
    confidence: float     # 0.0 - 1.0
    notes: str = ""       # short rationale


@dataclass
class PatchProposal:
    """
    A tiny, auditable patch suggestion. This agent NEVER writes files.
    Other system actors (Creator / Orchestrator) can consume this safely.
    """
    change: str                 # short id, e.g. "add_logging_guard"
    rationale: str              # why this helps
    target: str                 # function, class or file path hint
    patch_lines: List[str]      # human-readable snippet to apply
    meta: Dict[str, Any] = None # optional, for tooling hints

    def to_json(self) -> str:
        return json.dumps(
            {
                "change": self.change,
                "rationale": self.rationale,
                "target": self.target,
                "patch": self.patch_lines,
                "meta": self.meta or {},
            },
            indent=2,
        )


# ------------------------------ Policy -------------------------------- #

class Policy:
    """Very small guardrail layer for bounded execution."""
    def __init__(self, allow_network: bool = False, max_depth: int = 2):
        self.allow_network = allow_network
        self.max_depth = max_depth

    def validate(self, request: Dict[str, Any]) -> Tuple[bool, str]:
        depth = int(request.get("depth", 0) or 0)
        if depth > self.max_depth:
            return False, "depth_exceeded"
        wants_network = bool(request.get("network") or request.get("plan", {}).get("network"))
        if wants_network and not self.allow_network:
            return False, "network_forbidden"
        return True, "ok"


# ----------------------- Privacy-Aware Memory -------------------------- #

_SECRET_PATTERNS = [
    # API-style keys/tokens
    (re.compile(r"(?i)(api[_-]?key|token|secret|passwd|password)\s*[:=]\s*([^\s'\";]+)"), r"\1=<redacted>"),
    # Bearer tokens / Authorization headers
    (re.compile(r"(?i)(authorization)\s*:\s*bearer\s+[A-Za-z0-9\-._~\+\/]+=*"), r"\1: Bearer <redacted>"),
    # sk- prefixed keys (e.g., OpenAI-style)
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "<redacted-key>"),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    # Long hex/alnum sequences that look like secrets (heuristic: 24+ chars, mixed)
    (re.compile(r"\b[a-zA-Z0-9_\-]{24,}\b"), "<redacted>"),
    # Email addresses
    (re.compile(r"[\w\.-]+@[\w\.-]+\.\w+"), "<redacted-email>"),
    # Query params with tokens
    (re.compile(r"([?&](?:token|key|signature)=[^&#\s]+)", re.I), r"\1<redacted>"),
]

def _redact_secrets(text: str, max_len: int = 4000) -> str:
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    if len(out) > max_len:
        out = out[:max_len] + " …[truncated]"
    return out


class MemoryAdapter:
    """
    Thin adapter. The outer system injects callables:
      - writer(event: dict) -> None
      - reader(query: dict) -> str
    Both are optional; this adapter is defensive and never raises.

    Privacy: `log()` redacts common secrets before writing.
    """
    def __init__(
        self,
        writer: Optional[Callable[[Dict[str, Any]], None]] = None,
        reader: Optional[Callable[[Dict[str, Any]], str]] = None,
    ):
        self._writer = writer
        self._reader = reader

    def log(self, title: str, note: str, tag: str = "agent", level: str = "info") -> None:
        safe_note = _redact_secrets(note or "")
        event = {
            "title": title,
            "note": safe_note,
            "tag": tag,
            "level": level,
            "ts": time.time(),
            "module": "autonomous_agent",
            "version": __version__,
        }
        if callable(self._writer):
            try:
                self._writer(event)
            except Exception:
                # Never crash because memory failed.
                pass

    def read_recent(self, limit: int = 20, tag: Optional[str] = None) -> List[str]:
        if callable(self._reader):
            try:
                payload = {"limit": int(limit)}
                if tag:
                    payload["tag"] = tag
                txt = self._reader(payload)
                if isinstance(txt, str):
                    return [ln for ln in txt.splitlines() if ln.strip()]
            except Exception:
                pass
        return []


# -------------------------- Agent Core Logic -------------------------- #

class AutonomousAgent:
    """
    Minimal, testable agent:
    - Validates a request against Policy
    - Deterministically scores a plan to decide proceed/revise
    - Proposes a tiny, deterministic PatchProposal when 'revise'
    - Emits memory logs via MemoryAdapter (if wired)
    """
    def __init__(self, policy: Policy, memory: Optional[MemoryAdapter] = None):
        self.policy = policy
        self.memory = memory or MemoryAdapter()

    # ---- Scoring ----
    def evaluate(self, plan: Dict[str, Any]) -> float:
        """
        Deterministic score in [0,1]:
        - penalize network use (-0.30)
        - penalize high risk (-0.40), medium (-0.15)
        - small bonus per step (+0.05 up to +0.30)
        - small bonus if plan lists explicit tests (+0.10)
        """
        score = 1.0

        if plan.get("network"):
            score -= 0.30

        risk = (plan.get("risk") or "").lower()
        if risk == "high":
            score -= 0.40
        elif risk == "medium":
            score -= 0.15

        steps = plan.get("steps") or []
        score += min(0.30, 0.05 * len(steps))

        if plan.get("tests"):
            score += 0.10

        # clamp
        if score < 0.0:
            score = 0.0
        if score > 1.0:
            score = 1.0
        return score

    # ---- Decisioning ----
    def decide(self, request: Dict[str, Any]) -> Decision:
        ok, why = self.policy.validate(request)
        if not ok:
            self.memory.log("policy_block", f"blocked: {why}", "policy", "rejected")
            return Decision(action="reject", confidence=0.90, notes=why)

        confidence = self.evaluate(request.get("plan", {}))
        action = "proceed" if confidence >= 0.60 else "revise"
        # Defensive log: ensure memory adapter presence does not crash decisioning.
        try:
            self.memory.log(
                "decision",
                f"{action}@{confidence:.2f} (risk={request.get('plan',{}).get('risk','n/a')})",
                "agent",
                "success",
            )
        except Exception:
            # Swallow logging errors to keep decision flow stable.
            pass
        return Decision(action=action, confidence=confidence, notes="auto-evaluated")

    # ---- Patch Proposals ----
    def propose_patch(self, current_code: str) -> PatchProposal:
        """
        Return a tiny, deterministic patch suggestion so other agents can audit easily.
        This does NOT modify files—only proposes changes.
        """
        lines = [
            "# Guard: avoid AttributeError if memory adapter lacks 'log' or is None",
            "if not hasattr(self.memory, 'log') or not callable(getattr(self.memory, 'log', None)):",
            "    return Decision(action='revise', confidence=0.50, notes='no-memory-logger')",
        ]
        return PatchProposal(
            change="add_logging_guard",
            rationale="Ensure decision flow is resilient even if memory logger is unavailable.",
            target="AutonomousAgent.decide",
            patch_lines=lines,
            meta={"module": "autonomous_agent", "version": __version__},
        )

    # ---- Orchestrator-friendly runner ----
    def run_once(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Single pass: decide + optional patch."""
        decision = self.decide(request)
        out: Dict[str, Any] = {"version": __version__, "decision": asdict(decision)}
        if decision.action == "revise":
            out["patch"] = json.loads(self.propose_patch(request.get("current_code", "")).to_json())
        return out


# ------------------------------ Entrypoints --------------------------- #

def make_agent(policy: Optional[Policy] = None, memory: Optional[MemoryAdapter] = None) -> AutonomousAgent:
    """
    Factory to construct an AutonomousAgent with safe defaults.

    Note on explicit consent: This module implements an explicit-consent loader
    API to gate any potential heavy initialization. Because this single-file
    implementation is lightweight and side-effect-free, make_agent will work
    regardless of loader state. If future versions add heavy initialization,
    callers should call load_core(consent=True) or set AGENT_LOAD_CONSENT to
    enable such behavior.
    """
    policy = policy if policy is not None else Policy()
    memory = memory if memory is not None else MemoryAdapter()
    return AutonomousAgent(policy=policy, memory=memory)


def run(request_json: str) -> str:
    """
    JSON API for external callers.

    Expected keys (optional unless noted):
      - allow_network: bool
      - max_depth: int
      - depth: int
      - network: bool
      - plan: {
          steps: [..],
          risk: "low"|"medium"|"high",
          network: bool,
          tests: [..]
        }
      - current_code: str

    Note: Memory hooks are not wired here; the outer system can instantiate
    AutonomousAgent(policy, MemoryAdapter(writer, reader)) for full functionality.

    This function remains safe to import and call without enabling the loader.
    """
    try:
        req = json.loads(request_json or "{}")
    except json.JSONDecodeError as e:
        return json.dumps(
            {
                "version": __version__,
                "error": "invalid_json",
                "message": f"Malformed JSON: {e.msg}",
                "pos": {"lineno": getattr(e, 'lineno', None), "colno": getattr(e, 'colno', None)},
            },
            indent=2,
        )

    pol = Policy(
        allow_network=bool(req.get("allow_network", False)),
        max_depth=int(req.get("max_depth", 2)),
    )
    agent = AutonomousAgent(pol, memory=MemoryAdapter())  # no-op memory by default
    result = agent.run_once(req)
    return json.dumps(result, indent=2)
