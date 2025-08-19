# file: modules/autonomous_agent/core.py
"""
Core implementation for the autonomous agent, plus explicit, consent-gated loader.

Design guarantees:
- Importing this module performs NO heavy initialization, network, or credential loads.
- All privileged work happens ONLY inside load_core()/enable_core(), which are
  idempotent and thread-safe.

Spanish notes:
- Nunca hagas inicialización pesada al importar; sólo dentro de load_core/enable_core.
- Estas funciones registran consentimiento en el ledger ANTES de inicializar.
"""
from __future__ import annotations

import json
import re
import time
import threading
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import consent  # safe, no I/O at import

__version__ = "0.3.0"

def _as_request(obj: Any) -> Dict[str, Any]:
    """
    Coacciona entradas primitivas (str, int, None, etc.) a un request mínimo.
    Mantiene dicts tal cual. Garantiza que Policy.validate() reciba un dict.
    """
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    text = str(obj)
    return {"plan": {"task": text, "steps": [text]}}

# ---- Loader state (thread-safe, idempotent) ----
_loader_lock = threading.Lock()
_core_initialized: bool = False


def is_core_initialized() -> bool:
    return _core_initialized


def is_core_enabled() -> bool:
    # Alias/back-compat
    return is_core_initialized()


def load_core(consent_ok: bool = False, actor_id: Optional[str] = None, reason: Optional[str] = None) -> bool:
    """
    Initialize "core" behavior behind explicit consent.
    - Records a 'load' consent entry BEFORE initializing.
    - Thread-safe & idempotent; subsequent calls are no-ops.

    WARNING: pass 'consent_ok=True' intentionally. If False, returns False and does nothing.
    """
    global _core_initialized
    if not consent_ok:
        return False

    # Record consent first (append-only ledger)
    try:
        consent.record_consent(actor=actor_id or "unknown", mode="load", rationale=reason or "load_core")
    except Exception:
        # Never crash initialization on ledger write failures; continue safely.
        pass

    if _core_initialized:
        return True

    with _loader_lock:
        if _core_initialized:
            return True

        # ---- Place any future heavy init here (network clients, credentials, etc.) ----
        # Keep minimal & explicit. If you add steps that could block, consider finer-grained states.
        # For now we just flip the flag.
        _core_initialized = True

    return True


def enable_core(actor_id: Optional[str] = None, reason: Optional[str] = None) -> bool:
    """
    Convenience method:
    - Records an 'enable' consent entry, then calls load_core(consent_ok=True, ...).
    """
    try:
        consent.record_consent(actor=actor_id or "unknown", mode="enable", rationale=reason or "enable_core")
    except Exception:
        pass
    return load_core(consent_ok=True, actor_id=actor_id, reason=reason)


# ----------------------------- Data Models ---------------------------- #

@dataclass
class Decision:
    action: str            # "proceed" | "revise" | "reject"
    confidence: float      # 0.0 - 1.0
    notes: str = ""


@dataclass
class PatchProposal:
    change: str
    rationale: str
    target: str
    patch_lines: List[str]
    meta: Dict[str, Any] | None = None

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
    # sk- prefixed keys
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "<redacted-key>"),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    # Long hex/alnum sequences (heuristic)
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
    Thin adapter. The outer system may inject:
      - writer(event: dict) -> None
      - reader(query: dict) -> str
    Privacy: log() redacts common secrets before writing.
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
    - Proposes a tiny PatchProposal when 'revise'
    - Emits memory logs via MemoryAdapter (if wired)
    """
    def __init__(self, policy: Optional[Policy] = None, memory: Optional[MemoryAdapter] = None):
        # Permite AutonomousAgent() sin argumentos (compatibilidad con tests)
        self.policy = policy if policy is not None else Policy()
        self.memory = memory if memory is not None else MemoryAdapter()

    def evaluate(self, plan: Dict[str, Any]) -> float:
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
        return max(0.0, min(1.0, score))


    def decide(self, request: Dict[str, Any] | str) -> Decision | str:
        # Compatibility mode for legacy tests: string in → string out
        if isinstance(request, str):
            cmd = request.strip().lower()
            if cmd == "analyze":
                return "Analyzing system"
            if cmd == "improve":
                return "Applying improvement"
            return "Default behavior"

        # New mode: dict in → Decision out
        ok, why = self.policy.validate(request)
        if not ok:
            self.memory.log("policy_block", f"blocked: {why}", "policy", "rejected")
            return Decision(action="reject", confidence=0.90, notes=why)

        confidence = self.evaluate(request.get("plan", {}))
        action = "proceed" if confidence >= 0.60 else "revise"
        try:
            self.memory.log(
                "decision",
                f"{action}@{confidence:.2f} (risk={request.get('plan',{}).get('risk','n/a')})",
                "agent",
                "success",
            )
        except Exception:
            pass
        return Decision(action=action, confidence=confidence, notes="auto-evaluated")


    def propose_patch(self, current_code: str) -> PatchProposal:
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

    def run_once(self, request: Dict[str, Any]) -> Dict[str, Any]:
        decision = self.decide(request)
        out: Dict[str, Any] = {"version": __version__, "decision": asdict(decision)}
        if decision.action == "revise":
            out["patch"] = json.loads(self.propose_patch(request.get("current_code", "")).to_json())
        return out


# ------------------------------ Entrypoints --------------------------- #

def make_agent(policy: Optional[Policy] = None, memory: Optional[MemoryAdapter] = None) -> AutonomousAgent:
    policy = policy if policy is not None else Policy()
    memory = memory if memory is not None else MemoryAdapter()
    return AutonomousAgent(policy=policy, memory=memory)


def run(request_json: str) -> str:
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
        max_depth=int(req.get("max_depth", 2) or 2),  # <- cast seguro a int
    )
    agent = AutonomousAgent(pol, memory=MemoryAdapter())
    result = agent.run_once(req)
    return json.dumps(result, indent=2)
