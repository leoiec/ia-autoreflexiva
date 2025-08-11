# core/memory/summarizer.py
from __future__ import annotations

import os
from typing import List, Dict, Any
from crewai import LLM

# ---------- LLM builder ----------

def build_local_llm() -> LLM:
    """
    Returns a CrewAI LLM instance for summarization.
    Defaults to LM Studio local; can be overridden via env vars.

    Env vars:
      SUMMARY_PROVIDER=lm_studio|openai
      SUMMARY_MODEL_ID=deepseek-coder-6.7b-instruct  (or gpt-4o-mini if provider=openai)
      LM_STUDIO_BASE_URL=http://localhost:1234/v1
      LM_STUDIO_API_KEY=lmstudio-key   (dummy ok)
      OPENAI_API_KEY=...               (if provider=openai)
    """
    
    # OpenAI path (cheap & reliable for summarization)
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY not set but SUMMARY_PROVIDER=openai")
    return LLM(
        model="gpt-5-mini",
        api_key=openai_key
    )

# ---------- Summarizer ----------

SYSTEM_SUMMARY_INSTRUCTIONS = (
    "You summarize past agent outputs into a compact, actionable memory. "
    "Goal: prevent repetition, capture key decisions, blockers, and next steps. "
    "Be concrete, 6–10 bullets max. No apologies, no meta-instructions."
)

def _call_llm_safe(llm: LLM, prompt: str) -> str:
    """
    Try different call patterns to avoid 'object has no attribute invoke' errors.
    """
    try:
        # Most CrewAI LLM adapters implement `.call(prompt: str)`
        return llm.call(prompt)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        # Some adapters expose `.generate(prompt: str)`
        return llm.generate(prompt)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        # Some act like chat-completions
        msgs = [{"role": "system", "content": SYSTEM_SUMMARY_INSTRUCTIONS},
                {"role": "user", "content": prompt}]
        return llm.chat(messages=msgs)  # type: ignore[attr-defined]
    except Exception as e:
        # Final fallback: return empty to let caller handle it
        return f"[summary_error] {e}"

def _format_events_for_prompt(events: List[Dict[str, Any]], limit_chars: int = 3200) -> str:
    """Format a compact list of recent events for the LLM, capped by chars."""
    lines = []
    for e in events:
        tag = e.get("tag", "?")
        title = e.get("title", "event")
        note = (e.get("note", "") or "").strip().replace("\n\n", "\n")
        # trim long notes
        if len(note) > 400:
            note = note[:400] + " …[truncated]"
        lines.append(f"- [{tag}] {title}: {note}")
        if sum(len(x) for x in lines) > limit_chars:
            lines.append("…[truncated]")
            break
    return "\n".join(lines)

def summarize_events(events: List[Dict[str, Any]], llm: LLM) -> str:
    """
    Summarize recent memory events into a short bullet list.
    Resilient to LLM adapter differences.
    """
    if not events:
        return "No recent events to summarize."

    compact = _format_events_for_prompt(events)
    prompt = (
        f"{SYSTEM_SUMMARY_INSTRUCTIONS}\n\n"
        f"RECENT EVENTS (most recent first):\n{compact}\n\n"
        "Now output a compact memory summary in bullet points:"
    )

    resp = _call_llm_safe(llm, prompt)
    if not resp or resp.startswith("[summary_error]"):
        # Heuristic fallback: extract top-N signals
        top = []
        for e in events[:10]:
            tag = e.get("tag", "")
            title = e.get("title", "")
            note = (e.get("note", "") or "").strip().split("\n")[0]
            if len(note) > 160:
                note = note[:160] + "…"
            top.append(f"- [{tag}] {title}: {note}")
        return "Heuristic summary (LLM failed):\n" + "\n".join(top)

    return resp.strip()
