# file: tools/backlog_lib.py
"""
Librería mínima para manejar un backlog append-only en JSONL.

- Formato: repo/state/backlog.jsonl (una línea = un dict JSON)
- Cada actualización de un ítem NO sobreescribe: siempre se agrega una nueva línea
  con el mismo `id` y campos actualizados (p.ej. nuevo `status`, `note`, etc.).

Estados recomendados: "todo" -> "in_progress" -> "review" -> "done" | "blocked"
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Ruta por defecto (puedes cambiarla por env var)
DEFAULT_BACKLOG = os.getenv("BACKLOG_PATH", os.path.join("repo", "state", "backlog.jsonl"))

# Asegura carpeta
def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def _utc_epoch() -> float:
    return time.time()

def _ts_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def stable_digest(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]  # corto, suficiente

@dataclass
class BacklogItem:
    id: str
    kind: str
    title: str
    status: str = "todo"
    owner: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    rule: Optional[str] = None
    source: Optional[str] = None  # "flake8" | "mypy" | "pytest" | "pip" | ...
    note: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

    def to_jsonl(self) -> str:
        now = _ts_iso()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        return json.dumps(asdict(self), ensure_ascii=False)

def _append_line(path: str, line: str) -> None:
    _ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.strip() + "\n")

def append_item(item: BacklogItem, path: str = DEFAULT_BACKLOG) -> None:
    """Append una nueva línea con el item. No deduplica."""
    _append_line(path, item.to_jsonl())

def iter_items(path: str = DEFAULT_BACKLOG) -> Iterable[Dict[str, Any]]:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception:
                # línea corrupta → sáltala (backlog es tolerante)
                continue

def latest_by_id(path: str = DEFAULT_BACKLOG) -> Dict[str, Dict[str, Any]]:
    """Retorna el último estado de cada id."""
    last: Dict[str, Dict[str, Any]] = {}
    for obj in iter_items(path):
        _id = obj.get("id")
        if _id:
            last[_id] = obj
    return last

def find_open_by_digest(kind: str, file: str, line: Optional[int], rule: Optional[str],
                        title: str, path: str = DEFAULT_BACKLOG) -> Optional[Dict[str, Any]]:
    """Busca un item 'abierto' que coincida con el mismo digest estable."""
    digest = stable_digest(kind, file or "", str(line or 0), rule or "", title or "")
    last = latest_by_id(path)
    for obj in last.values():
        if obj.get("meta", {}).get("digest") == digest and obj.get("status") not in ("done", "blocked"):
            return obj
    return None

def create_or_reopen(kind: str, title: str, owner: str, file: Optional[str],
                     line: Optional[int], rule: Optional[str], source: str,
                     note: Optional[str] = None, path: str = DEFAULT_BACKLOG) -> str:
    """Crea un nuevo TODO si no existe uno abierto con el mismo digest; si existe, agrega una línea cambiando status a 'todo'."""
    digest = stable_digest(kind, file or "", str(line or 0), rule or "", title or "")
    existing = find_open_by_digest(kind, file or "", line, rule, title, path)
    _id = existing["id"] if existing else f"T-{digest}"
    item = BacklogItem(
        id=_id,
        kind=kind,
        title=title,
        status="todo",
        owner=owner,
        file=file,
        line=line,
        rule=rule,
        source=source,
        note=note,
        meta={"digest": digest},
    )
    append_item(item, path)
    return _id

def update_status(task_id: str, status: str, note: Optional[str] = None,
                  changes: Optional[List[Dict[str, Any]]] = None,
                  path: str = DEFAULT_BACKLOG) -> None:
    """Agrega una línea actualizando el estado."""
    last = latest_by_id(path).get(task_id)
    if not last:
        # crea placeholder si no existía
        base = BacklogItem(id=task_id, kind="misc", title=f"update {task_id}", status=status, note=note, meta={"changes": changes or []})
        append_item(base, path)
        return
    # Hereda campos clave
    item = BacklogItem(
        id=task_id,
        kind=last.get("kind", "misc"),
        title=last.get("title", f"update {task_id}"),
        status=status,
        owner=last.get("owner"),
        file=last.get("file"),
        line=last.get("line"),
        rule=last.get("rule"),
        source=last.get("source"),
        note=note,
        meta={**(last.get("meta") or {}), **({"changes": changes} if changes else {})},
    )
    append_item(item, path)

def pick_batch(limit: int = 5, kinds: Optional[List[str]] = None,
               owners: Optional[List[str]] = None, path: str = DEFAULT_BACKLOG) -> List[Dict[str, Any]]:
    """Devuelve hasta `limit` tareas con status 'todo' filtradas."""
    last = latest_by_id(path).values()
    pool = [o for o in last if o.get("status") == "todo"]
    if kinds:
        pool = [o for o in pool if o.get("kind") in kinds]
    if owners:
        pool = [o for o in pool if o.get("owner") in owners]
    # FIFO aproximado usando created_at
    pool.sort(key=lambda o: o.get("created_at", ""))
    return pool[:limit]
