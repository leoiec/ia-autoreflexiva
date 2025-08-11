# tools/memory_tool.py
from typing import Optional, Type, Any, Dict
from pydantic import BaseModel, Field
from crewai.tools import BaseTool  # OJO: desde crewai (no crewai_tools)
from core.memory.memory_store import MemoryStore
import json

# -------------------------- Helpers --------------------------

def _coerce_to_dict(obj: Any) -> Dict[str, Any]:
    """
    Best-effort:
    - If str -> try json.loads
    - If list -> use first element if it's a dict, otherwise {}
    - If dict -> return a shallow copy
    - Else -> {}
    """
    if obj is None:
        return {}
    try:
        if isinstance(obj, str):
            parsed = json.loads(obj)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        return dict(item)
                return {}
            if isinstance(parsed, dict):
                return dict(parsed)
            return {}
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    return dict(item)
            return {}
        if isinstance(obj, dict):
            return dict(obj)
    except Exception:
        return {}
    return {}

def _merge_kwargs_with_flex(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge kwargs with any flexible fields ('query', 'payload', 'data') that may
    contain a dict / list / json string. LLMs a veces mandan arrays o strings.
    """
    merged = dict(kwargs)
    for key in ("query", "payload", "data"):
        if key in merged and merged[key] is not None:
            coerced = _coerce_to_dict(merged[key])
            for k, v in coerced.items():
                if k not in merged or merged[k] is None:
                    merged[k] = v
    return merged

# -------------------------- Schemas --------------------------

class _WriteArgsFlexible(BaseModel):
    # Canónicos
    title: Optional[str] = Field(default=None, description="Short title of the memory event")
    note: Optional[str]  = Field(default=None, description="Concise note with the decision/rationale/outcome")
    tag: Optional[str]   = Field(default="general", description="Category tag for the event")

    # Opcionales adicionales (si tu MemoryStore los soporta, se guardan tal cual)
    level: Optional[str]    = Field(default=None, description="Event level, e.g., info|success|error|rejected")
    cycle_id: Optional[str] = Field(default=None, description="Cycle identifier to correlate events")

    # Campos flexibles para tolerancia de entrada
    query: Optional[Any]   = Field(default=None, description="Flexible input (str/list/dict) to parse")
    payload: Optional[Any] = Field(default=None, description="Flexible input (str/list/dict) to parse")
    data: Optional[Any]    = Field(default=None, description="Flexible input (str/list/dict) to parse")

    class Config:
        json_schema_extra = {"description": "Use an object like {'title': '...', 'note': '...', 'tag': '...'}"}

class _ReadArgsFlexible(BaseModel):
    # Canónicos
    limit: Optional[int] = Field(default=10, description="Max number of recent events to read")

    # Filtros opcionales (se filtran client-side si MemoryStore no los soporta)
    tag: Optional[str]   = Field(default=None, description="Filter by tag")
    level: Optional[str] = Field(default=None, description="Filter by level")

    # Campos flexibles para tolerancia de entrada
    query: Optional[Any] = Field(default=None, description="Flexible input (str/list/dict) to parse")

    class Config:
        json_schema_extra = {"description": "Use an object like {'limit': 50, 'tag': 'auditor'}"}

# -------------------------- Tools --------------------------

class MemoryWriteTool(BaseTool):
    name: str = "memory_write"
    description: str = (
        "Append an event to the shared system memory. "
        "Use this to persist key decisions, risks, mitigations or proposals so future cycles inherit them.\n"
        "Input MUST be a JSON object like "
        '{"title":"string","note":"string","tag":"string","level":"info","cycle_id":"..."}'
    )
    args_schema: Type[BaseModel] = _WriteArgsFlexible  # Pydantic v2: declarar el tipo

    def _run(
        self,
        title: Optional[str] = None,
        note: Optional[str] = None,
        tag: Optional[str] = "general",
        level: Optional[str] = None,
        cycle_id: Optional[str] = None,
        query: Optional[Any] = None,
        payload: Optional[Any] = None,
        data: Optional[Any] = None,
    ) -> str:
        # Merge flexible inputs
        merged = _merge_kwargs_with_flex(locals())

        _title = merged.get("title")
        _note  = merged.get("note")
        _tag   = merged.get("tag", "general")
        _level = merged.get("level")
        _cycle = merged.get("cycle_id")

        if not _title or not _note:
            return (
                "Error: missing required fields. Expected JSON object like "
                '{"title":"string","note":"string","tag":"string","level":"info","cycle_id":"..."}'
            )

        event = {"title": _title, "note": _note, "tag": _tag}
        if _level:
            event["level"] = _level
        if _cycle:
            event["cycle_id"] = _cycle

        try:
            mem = MemoryStore()
            mem.add_event(event)
            return "ok"
        except Exception as e:
            return f"error: {e}"

class MemoryReadTool(BaseTool):
    name: str = "memory_read"
    description: str = (
        "Read recent events from the shared system memory. "
        "Use this before proposing changes to avoid repetition and build on past decisions.\n"
        "Input MUST be a JSON object like {\"limit\": 50} or {\"limit\": 100, \"tag\": \"auditor\"}."
    )
    args_schema: Type[BaseModel] = _ReadArgsFlexible

    def _run(
        self,
        limit: Optional[int] = 10,
        tag: Optional[str] = None,
        level: Optional[str] = None,
        query: Optional[Any] = None,
    ) -> str:
        # Merge flexible input into kwargs
        merged = _merge_kwargs_with_flex(locals())

        _limit = merged.get("limit", 10)
        try:
            _limit = int(_limit)
        except Exception:
            _limit = 10

        _tag   = merged.get("tag")
        _level = merged.get("level")

        mem = MemoryStore()

        # Carga básica; si MemoryStore no soporta filtros, filtramos aquí
        try:
            events = mem.load_events(limit=_limit if _limit and _limit > 0 else 10)
        except TypeError:
            events = mem.load_events(limit=_limit if _limit and _limit > 0 else 10)

        # Filtrado client-side
        if _tag:
            events = [e for e in events if e.get("tag") == _tag]
        if _level:
            events = [e for e in events if e.get("level") == _level]

        if not events:
            return "(no events)"

        lines = []
        for e in events:
            tag_val = e.get("tag", "general")
            lvl_val = e.get("level")
            title   = e.get("title", "event")
            note    = e.get("note", "")
            prefix  = f"[{tag_val}]" + (f"[{lvl_val}]" if lvl_val else "")
            lines.append(f"- {prefix} {title}: {note}")

        return "\n".join(lines)
