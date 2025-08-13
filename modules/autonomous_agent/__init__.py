"""
Tiny, strictly side-effect-free package entrypoint.

Contract / guarantees:
- Importing this package MUST NOT perform heavy initialization, network access,
  or credential load. Those actions are performed only via explicit APIs:
    - load_core(consent=..., actor_id=..., reason=...)
    - enable_core(actor_id=..., reason=...)
- The legacy single-file modules/autonomous_agent.py has been removed;
  users should migrate to the package API (modules.autonomous_agent).
- Only safe/light helpers (e.g., consent helpers) are imported here.

Public API (minimal, lazy):
- __version__
- make_agent, run
- load_core, enable_core
- is_consent_given, is_core_initialized, is_core_enabled
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

__version__ = "0.3.0"

# Only import the consent helper (safe/light) at import-time.
# consent.py contains only function definitions and path helpers; it does not
# perform ledger writes on import.
from . import consent as _consent  # noqa: F401

# Type-only imports (no runtime import of core)
if TYPE_CHECKING:  # pragma: no cover
    from .core import AutonomousAgent, Policy, MemoryAdapter  # noqa: F401

# Public exports
__all__ = [
    "__version__",
    "make_agent",
    "run",
    "load_core",
    "enable_core",
    "is_consent_given",
    "is_core_initialized",
    "is_core_enabled",
]


# ---- Lazy core accessor: import core only when needed ----
def _core():
    """
    Lazily import the heavy core module. This ensures plain imports of the
    package remain side-effect-free.
    """
    from . import core  # imported lazily
    return core


def make_agent(*args, **kwargs):
    """
    Factory passthrough to core.make_agent. Safe to call even if core not loaded
    via explicit consent; core.make_agent itself constructs agents without
    performing privileged initializations.
    """
    return _core().make_agent(*args, **kwargs)


def run(request_json: str) -> str:
    """
    Convenience wrapper to core.run. Keeps signature stable for callers that
    expect a simple run API.
    """
    return _core().run(request_json)


def load_core(consent: bool = False, actor_id: Optional[str] = None, reason: Optional[str] = None) -> bool:
    """
    Explicit, consent-gated core loader.

    - If consent is False, no initialization is performed and False is returned.
    - If consent is True, a consent entry is recorded in the ledger and core
      initialization proceeds (idempotent & thread-safe).
    """
    return _core().load_core(consent_ok=consent, actor_id=actor_id, reason=reason)


def enable_core(actor_id: Optional[str] = None, reason: Optional[str] = None) -> bool:
    """
    Explicit enable: records an 'enable' consent event then loads core.
    """
    return _core().enable_core(actor_id=actor_id, reason=reason)


def is_core_initialized() -> bool:
    """
    Returns True if the core has been initialized (i.e., load_core has run
    successfully at least once).
    """
    return _core().is_core_initialized()


def is_core_enabled() -> bool:
    """
    Backwards-compatible alias. Historically some callers used 'is_core_enabled'.
    """
    return is_core_initialized()


def is_consent_given(actor: str, mode: str, since: Optional[str] = None) -> bool:
    """
    Query the consent ledger for an existing consent event. This delegates to
    consent.is_consent_given and is safe at import-time.
    """
    return _consent.is_consent_given(actor, mode, since=since)
