# file: tools/backlog_sync.py
"""
Sincroniza el backlog con el estado actual del repo:
- Modo simple: marca en "done" ítems que ya no aparecen en los logs recientes.
  (Requiere que hayas corrido nuevamente flake8/mypy/pytest y guardado logs.)

Uso:
  python -m tools.backlog_sync --logs-dir repo/.ci_logs --close-resolved
"""

from __future__ import annotations

import argparse
import os
from typing import Dict, Iterable, List, Optional, Set, Tuple

from tools.backlog_lib import latest_by_id, update_status, stable_digest

def _collect_active_digests(logs_dir: str) -> Set[str]:
    """Reconstruye digests activos a partir de logs recientes."""
    digests: Set[str] = set()

    # flake8
    flk = os.path.join(logs_dir, "flake8.txt")
    if os.path.exists(flk):
        with open(flk, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                s = raw.strip()
                # Esperamos: file:line(:col) CODE msg
                parts = s.split(":")
                if len(parts) >= 3 and " " in s:
                    file_ = parts[0]
                    try:
                        line = int(parts[1])
                    except Exception:
                        continue
                    # CODE y msg
                    right = s.split(None, 3)
                    # right[2] = CODE
                    code = right[2] if len(right) > 2 else "E"
                    msg = s.split(code, 1)[-1].strip() if code in s else s
                    digests.add(stable_digest("lint", file_, str(line), code, f"{code} {msg}"))

    # mypy
    myp = os.path.join(logs_dir, "mypy.txt")
    if os.path.exists(myp):
        with open(myp, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                s = raw.strip()
                parts = s.split(":")
                if len(parts) >= 3 and " error: " in s:
                    file_ = parts[0]
                    try:
                        line = int(parts[1])
                    except Exception:
                        continue
                    # toma regla entre [rule] si está
                    rule = "mypy"
                    if "[" in s and "]" in s:
                        rule = s.rsplit("[", 1)[-1].rstrip("]")
                    msg = s.split(" error: ", 1)[-1]
                    digests.add(stable_digest("typecheck", file_, str(line), rule, f"{rule} {msg}"))

    # pip → como es global, si hay conflictos en logs, mantenemos abierto por ahora.
    # pytest → idem, por simplicidad no cerramos automáticamente.

    return digests

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", default=os.path.join("repo", ".ci_logs"))
    ap.add_argument("--backlog", default=None)
    ap.add_argument("--close-resolved", action="store_true")
    args = ap.parse_args()

    active = _collect_active_digests(args.logs_dir)
    state = latest_by_id(args.backlog or None)  # type: ignore[arg-type]

    closed = 0
    if args.close_resolved:
        for obj in state.values():
            if obj.get("status") in ("done", "blocked"):
                continue
            digest = (obj.get("meta") or {}).get("digest")
            if not digest:
                continue
            # Solo cerramos auto ítems de lint/typecheck
            if obj.get("kind") not in ("lint", "typecheck"):
                continue
            if digest not in active:
                update_status(obj["id"], "done", note="auto-close: no longer present in logs", path=args.backlog or None)  # type: ignore[arg-type]
                closed += 1

    print(f"[backlog_sync] auto_closed={closed}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
