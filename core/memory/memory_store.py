# core/memory/memory_store.py
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional


class MemoryStore:
    """
    Simple JSON-backed memory store.
    Structure on disk:
    {
      "events": [
        {
          "title": str,
          "note": str,
          "tag": str,          # e.g., "architect", "creator", ...
          "level": str,        # "info" | "success" | "error"
          "cycle_id": str,
          "ts": ISO-8601 str
        },
        ...
      ],
      "summary": {"text": str, "ts": ISO-8601 str}
    }
    """

    def __init__(self, path: str = "data/memory.json") -> None:
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        if not os.path.exists(self.path):
            self._save_all({"events": [], "summary": {"text": "", "ts": ""}})

    # ----------------- internal i/o -----------------

    def _load_all(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                # recoverable default
                return {"events": [], "summary": {"text": "", "ts": ""}}

    def _save_all(self, data: Dict[str, Any]) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ----------------- public api -----------------

    def add_event(self, event: Dict[str, Any]) -> None:
        """
        Adds an event, auto-filling missing fields.
        Expected keys: title, note, tag, level, cycle_id, ts
        """
        data = self._load_all()
        e = dict(event or {})
        e.setdefault("title", "event")
        e.setdefault("note", "")
        e.setdefault("tag", "general")
        e.setdefault("level", "info")
        e.setdefault("cycle_id", "")
        e.setdefault("ts", datetime.utcnow().isoformat())
        data["events"].append(e)
        self._save_all(data)

    def load_events(
        self,
        limit: int = 50,
        tag: Optional[str] = None,
        level: Optional[str] = None,
        reverse: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Returns last N events, optionally filtered by tag/level.
        """
        data = self._load_all()
        events: List[Dict[str, Any]] = data.get("events", [])
        if reverse:
            events = list(reversed(events))
        filtered = []
        for ev in events:
            if tag and ev.get("tag") != tag:
                continue
            if level and ev.get("level") != level:
                continue
            filtered.append(ev)
            if len(filtered) >= limit:
                break
        return filtered

    def get_summary(self) -> Dict[str, str]:
        data = self._load_all()
        summary = data.get("summary") or {}
        # always ensure keys
        return {
            "text": summary.get("text", "") or "",
            "ts": summary.get("ts", "") or "",
        }

    def save_summary(self, text: str) -> None:
        data = self._load_all()
        data["summary"] = {"text": text or "", "ts": datetime.utcnow().isoformat()}
        self._save_all(data)

    def purge(
        self,
        levels_to_remove: Optional[List[str]] = None,
        note_contains: Optional[List[str]] = None,
        tags_to_remove: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """
        Deletes events whose 'level' is in levels_to_remove,
        or whose 'note' contains any substring in note_contains,
        or whose 'tag' is in tags_to_remove.

        Returns stats: {"before":N, "after":M, "removed":N-M}
        """
        levels_to_remove = set(levels_to_remove or [])
        tags_to_remove = set(tags_to_remove or [])
        substrs = note_contains or []

        data = self._load_all()
        original = data.get("events", [])
        cleaned = []
        for ev in original:
            if ev.get("level") in levels_to_remove:
                continue
            if ev.get("tag") in tags_to_remove:
                continue
            note = (ev.get("note") or "")
            if any(sub in note for sub in substrs):
                continue
            cleaned.append(ev)

        data["events"] = cleaned
        self._save_all(data)
        return {"before": len(original), "after": len(cleaned), "removed": len(original) - len(cleaned)}
