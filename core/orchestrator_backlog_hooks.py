# file: core/orchestrator_backlog_hooks.py
"""
Helpers para que el orquestador (o crew_config) interactúe con el backlog
sin acoplarse a la estructura interna del JSONL.

API estable y mínima:
- backlog_add_todo_from_error(...)
- backlog_mark_in_progress(task_id, note=None)
- backlog_mark_review(task_id, note=None, changes=None)
- backlog_mark_done(task_id, note=None)
- backlog_pick_todos(limit=5, kinds=None, owners=None)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.backlog_lib import (
    create_or_reopen,
    update_status,
    pick_batch,
)

def backlog_add_todo_from_error(*, kind: str, title: str, owner: str,
                                file: Optional[str], line: Optional[int],
                                rule: Optional[str], source: str) -> str:
    """
    Crea (o reabre) un 'todo' a partir de un error concreto.
    Retorna el task_id.
    """
    return create_or_reopen(
        kind=kind,
        title=title,
        owner=owner,
        file=file,
        line=line,
        rule=rule,
        source=source,
    )

def backlog_mark_in_progress(task_id: str, note: Optional[str] = None) -> None:
    update_status(task_id, "in_progress", note=note)

def backlog_mark_review(task_id: str, note: Optional[str] = None,
                        changes: Optional[List[Dict[str, Any]]] = None) -> None:
    update_status(task_id, "review", note=note, changes=changes)

def backlog_mark_done(task_id: str, note: Optional[str] = None) -> None:
    update_status(task_id, "done", note=note)

def backlog_pick_todos(limit: int = 5, kinds: Optional[List[str]] = None,
                       owners: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return pick_batch(limit=limit, kinds=kinds, owners=owners)
