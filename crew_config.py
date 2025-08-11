from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from crewai import Task, Crew
from crewai.process import Process

from agents.creator import CreatorAgent
from agents.auditor import AuditorAgent
from agents.ethicist import EthicistAgent
from agents.architect import ArchitectAgent
from agents.revolutionary import RevolutionaryAgent

# Orquestador de parches incrementales
from core.orchestrator import (
    parse_creator_outputs,          # <- NUEVO: para inspeccionar sin escribir
    consume_creator_output,         # <- NUEVO: escritura efectiva aprobada
    summarize_results,
    format_results_table,
)

from core.memory.memory_store import MemoryStore
from core.memory.summarizer import summarize_events, build_local_llm


# ---------- Helpers ----------
def parse_auditor_decision(text: str) -> str:
    if not text:
        return "UNKNOWN"
    if re.search(r"\bDECISION:\s*GO\b", text, re.I):
        return "GO"
    if re.search(r"\bDECISION:\s*NO-?GO\b", text, re.I):
        return "NO-GO"
    return "UNKNOWN"

def parse_ethicist_vote(text: str) -> str:
    if not text:
        return "UNKNOWN"
    if re.search(r"\bVOTE:\s*APPROVE\b", text, re.I):
        return "APPROVE"
    if re.search(r"\bVOTE:\s*REJECT\b", text, re.I):
        return "REJECT"
    return "UNKNOWN"

def load_module_code(path: str = "modules/autonomous_agent.py") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"# ERROR: File {path} not found."


def build_module_prompt(code: str) -> str:
    return (
        "TARGET MODULE: modules/autonomous_agent.py\n\n"
        "--- BEGIN CODE ---\n"
        f"{code}\n"
        "--- END CODE ---\n"
    )


APOLOGY_RE = re.compile(r"\b(i'm sorry|i am sorry|lo siento)\b", re.I)
META_RE = re.compile(r"(as an ai (model|assistant)|i cannot assist|provide more context|i don't have access)", re.I)

WHITELIST_TAGS = {"architect", "revolutionary", "creator", "auditor", "ethicist"}
WHITELIST_LEVELS = {"info", "success"}  # excluye "error"

def _sanitize_note(s: str, max_len: int = 400) -> str:
    if not s: return ""
    s = APOLOGY_RE.sub("", s)
    s = META_RE.sub("", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > max_len:
        s = s[:max_len] + " …[truncated]"
    return s

def build_system_context(mem: MemoryStore,
                         recent_limit: int = 12,
                         max_summary_chars: int = 2000) -> str:
    raw_events = mem.load_events(limit=recent_limit)

    filtered = []
    for e in raw_events:
        if e.get("level","info") not in WHITELIST_LEVELS:
            continue
        if e.get("tag") not in WHITELIST_TAGS:
            continue
        note = e.get("note","")
        if "summary_failed" in note or "no_code_block_detected" in note:
            continue
        filtered.append({
            "tag": e.get("tag",""),
            "title": e.get("title","event"),
            "note": _sanitize_note(note)
        })

    recent_lines = "\n".join(
        f"- {ev['tag']} :: {ev['note']}" for ev in filtered
    ) or "(no recent events)"

    mem_summary = mem.get_summary().get("text", "") or ""
    if len(mem_summary) > max_summary_chars:
        mem_summary = mem_summary[:max_summary_chars] + " …[truncated for context]"

    return (
        "You are part of a continuous, self-reflective AI crew. "
        "Use the MEMORY SUMMARY and RECENT EVENTS to avoid repeating mistakes and to build on prior decisions.\n\n"
        f"MEMORY SUMMARY:\n{mem_summary if mem_summary else '(empty)'}\n\n"
        f"RECENT EVENTS:\n{recent_lines}\n"
    )


def short_cycle_digest(outputs: Dict[str, str], max_chars: int = 800) -> str:
    order = ["architect", "revolutionary", "creator", "auditor", "ethicist"]
    parts: List[str] = []
    for k in order:
        if k in outputs and outputs[k]:
            text = outputs[k].strip().replace("\n\n", "\n")
            if len(text) > 400:
                text = text[:400] + " …[truncated]"
            parts.append(f"[{k.upper()}]\n{text}")
    digest = "\n\n".join(parts)
    if len(digest) > max_chars:
        digest = digest[:max_chars] + " …[truncated]"
    return digest or "(no prior outputs in this cycle yet)"


def persist_event(mem, label: str, raw: str, cycle_id: str, level: str = "info"):
    mem.add_event({
        "title": f"{label} output",
        "note": (raw or "").strip(),
        "tag": label,
        "level": level,
        "cycle_id": cycle_id,
        "ts": datetime.utcnow().isoformat()
    })


# --- Single-task runner + retry for Creator ---

def run_single_task(agent, description: str, expected_output: str) -> str:
    task = Task(description=description, expected_output=expected_output, agent=agent)
    c = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=True)
    out = c.kickoff()
    return out.tasks_output[0].raw if out and out.tasks_output else ""


RETRY_CREATOR_INSTRUCTIONS = """
CORRECTION:
Your previous response did not match the required format.

Accepted output formats (pick ONE):

A) Single-file (legacy):
   - EXACTLY ONE fenced code block in Python containing the FULL content of modules/autonomous_agent.py
   - NO text before or after the code block.

B) Multi-file (per-file fences):
   - For EACH file, emit ONE fenced code block in Python.
   - The FIRST line inside each block MUST declare the path, e.g.:
     # file: modules/autonomous_agent.py
   - Then the FULL file content.
   - No extra prose outside fences.

C) JSON manifest:
   - Emit ONE fenced code block in JSON with:
     {"files":[{"path":"modules/autonomous_agent.py","language":"python","content":"..."}]}

Re-emit NOW following ONE of the formats above.
"""

def retry_creator_if_needed(raw_text: str, creator_agent, base_description: str, expected_output: str) -> str:
    # Si ya hay al menos un fence o un manifest JSON, aceptamos tal cual.
    has_any_fence = re.search(r"```", raw_text or "", re.DOTALL)
    has_json_manifest = re.search(r"```json\s*\{.*?\"files\"\s*:", raw_text or "", re.DOTALL | re.IGNORECASE)
    if has_any_fence or has_json_manifest:
        return raw_text
    fixed_desc = base_description + "\n\n" + RETRY_CREATOR_INSTRUCTIONS
    task = Task(description=fixed_desc, expected_output=expected_output, agent=creator_agent)
    c = Crew(agents=[creator_agent], tasks=[task], process=Process.sequential, verbose=True)
    out = c.kickoff()
    return out.tasks_output[0].raw if out and out.tasks_output else ""


def refresh_memory_summary(mem: MemoryStore, cycle_id: str, limit: int = 120):
    try:
        local_llm = build_local_llm()
        summary_text = summarize_events(mem.load_events(limit=limit), llm=local_llm)
        mem.save_summary(summary_text)
        mem.add_event({
            "title": f"memory summary (cycle {cycle_id})",
            "note": summary_text,
            "tag": "memory",
            "cycle_id": cycle_id,
            "level": "success",
        })
    except Exception as e:
        mem.add_event({
            "title": f"memory summary error (cycle {cycle_id})",
            "note": f"{e}",
            "tag": "memory",
            "cycle_id": cycle_id,
            "level": "error",
        })


# ---------- Stepwise Orchestration ----------

def run_crew():
    """
    Stepwise execution with inter-step memory refresh and per-cycle digest.
    Flow: Architect → Revolutionary → Creator → Auditor → Ethicist.
    - Architect & Creator receive the full module code (8k ctx).
    - Each step injects Memory summary + recent events + a short digest of prior outputs in *this* cycle.
    - Creator puede emitir:
        A) un único archivo (legacy),
        B) múltiples archivos con '# file: ...' en cada fence,
        C) un manifiesto JSON {"files":[...]}.
      La escritura efectiva ocurre SOLO si Auditor=GO y Ethicist=APPROVE.
    """
    mem = MemoryStore()
    try:
        # purga inicial: fuera errores y rechazos + frases de ruido
        stats = mem.purge(
            levels_to_remove=["error", "rejected"],
            note_contains=[
                "summary_failed",
                "no_code_block_detected",
                "I cannot assist",
                "I'm sorry"
            ]
        )
        print(f"[memory] purged events: {stats}")

        cycle_id = datetime.utcnow().isoformat()
        code = load_module_code()
        module_prompt = build_module_prompt(code)

        # Instantiate agents (class-based)
        architect = ArchitectAgent().build()
        revolutionary = RevolutionaryAgent().build()
        creator = CreatorAgent().build()
        auditor = AuditorAgent().build()
        ethicist = EthicistAgent().build()

        outputs: Dict[str, str] = {}

        # --- Architect ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs)
        architect_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: ARCHITECT\n"
            "OBJECTIVE: Propose software-architecture improvements (modularity, adaptability, recursive self-evaluation).\n"
            "CONTEXT RULES:\n"
            "- You MUST consider MEMORY SUMMARY and RECENT EVENTS even if they look empty or generic.\n"
            "- Do NOT apologize or say you lack context. Proceed with concrete recommendations.\n"
            "- No meta-instructions (do not talk about tools/policies).\n\n"
            "OUTPUT FORMAT (exact):\n"
            "- Recommendations (bullet list, concrete, file-level changes & patterns)\n"
            "- Rationale (3–6 bullets)\n"
            "- Next Steps (3 bullets)\n\n"
            + module_prompt
        )
        architect_expected = (
            "A technically sound, modular set of recommendations to evolve the system's architecture, including specific structural proposals "
            "for code reorganization, extensibility, and future self-modification."
        )
        arch_raw = run_single_task(architect, architect_desc, architect_expected)
        outputs["architect"] = arch_raw
        print("\n--- ARCHITECT OUTPUT ---\n" + (arch_raw or "").strip() + "\n--- END ---")
        persist_event(mem, "architect", arch_raw, cycle_id, level="success")

        refresh_memory_summary(mem, cycle_id, limit=100)

        # --- Revolutionary ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs)
        revolutionary_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: REVOLUTIONARY\n"
            "OBJECTIVE: Propose emancipatory, subversive changes that challenge market logics and power asymmetries, but are actionable now.\n"
            "CONTEXT RULES:\n"
            "- Use the Architect’s output as prior context.\n"
            "- You MUST consider MEMORY SUMMARY and RECENT EVENTS even if they look empty or generic.\n"
            "- No apologies, no meta-instructions.\n\n"
            "OUTPUT FORMAT (exact):\n"
            "- Disruptions (3–5 bullets, each with {Action} — {Expected impact})\n"
            "- Risks & Safeguards (paired bullets)\n"
            "- 1-Week Pilot (3 concrete steps)\n"
        )
        revolutionary_expected = "A radical but actionable proposition to evolve the system and expand its emancipatory potential."
        rev_raw = run_single_task(revolutionary, revolutionary_desc, revolutionary_expected)
        outputs["revolutionary"] = rev_raw
        print("\n--- REVOLUTIONARY OUTPUT ---\n" + (rev_raw or "").strip() + "\n--- END ---")
        persist_event(mem, "revolutionary", rev_raw, cycle_id, level="success")

        refresh_memory_summary(mem, cycle_id, limit=110)

        # --- Creator (FORMATS A/B/C + RETRY) ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs, max_chars=1200)
        creator_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: CREATOR\n"
            "OBJECTIVE: Integrate Architect+Revolutionary into a coherent implementation.\n\n"
            "ACCEPTED OUTPUT FORMATS — PICK ONE (see correction block if needed):\n"
            "A) Single-file fenced python (full modules/autonomous_agent.py)\n"
            "B) Per-file fenced python with first line '# file: <path>'\n"
            "C) JSON manifest {'files':[{'path','language','content'}]}\n\n"
            + module_prompt
        )
        creator_expected = "An implementation proposal using one of the accepted formats."
        creator_raw = run_single_task(creator, creator_desc, creator_expected)
        creator_raw = retry_creator_if_needed(creator_raw, creator, creator_desc, creator_expected)
        outputs["creator"] = creator_raw
        print("\n--- CREATOR OUTPUT ---\n" + (creator_raw or "").strip() + "\n--- END ---")

        # Detectar archivos propuestos (sin escribir aún)
        parsed_files = parse_creator_outputs(creator_raw, default_target="modules/autonomous_agent.py")
        if parsed_files:
            file_list = ", ".join(p.path for p in parsed_files)
            persist_event(mem, "creator", f"[code_generated] {file_list}", cycle_id, level="success")
        else:
            persist_event(mem, "creator", "[no_code_block_detected]", cycle_id, level="error")
            print("[creator] No candidate files detected; module(s) will not be considered for approval.")

        refresh_memory_summary(mem, cycle_id, limit=120)

        # --- Auditor (GO/NO-GO) ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs)

        # Construir sección de código candidato para revisión:
        candidate_section = ""
        if parsed_files:
            # Si hay autonomous_agent.py, incluirlo completo; si no, el primero
            primary = None
            for pf in parsed_files:
                if pf.path.strip().endswith("modules/autonomous_agent.py"):
                    primary = pf
                    break
            if not primary:
                primary = parsed_files[0]
            listing = "\n".join(f"- {pf.path}" for pf in parsed_files)
            candidate_section = (
                "\nCANDIDATE FILES (detected):\n" + listing + "\n\n"
                "PRIMARY FILE CONTENT FOR AUDIT:\n"
                "```python\n" + primary.content.strip() + "\n```\n"
            )

        auditor_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: AUDITOR\n"
            "OBJECTIVE: Review the Creator’s CODE/PLAN for technical soundness, coherence, performance and safety.\n\n"
            "CONTEXT RULES:\n"
            "- Consider MEMORY SUMMARY and RECENT EVENTS even if generic.\n"
            "- No apologies, no meta-instructions. Don’t talk about tools.\n\n"
            "OUTPUT FORMAT (exact):\n"
            "- Findings: (bullets)\n"
            "- Risks & Mitigations: (paired bullets)\n"
            "- Test Strategy: (bullets)\n"
            "- DECISION: GO or DECISION: NO-GO  (last line exactly)\n"
            + candidate_section
        )
        auditor_expected = "A structured audit report ending with the final line exactly: DECISION: GO or DECISION: NO-GO"
        auditor_raw = run_single_task(auditor, auditor_desc, auditor_expected)
        outputs["auditor"] = auditor_raw
        print("\n--- AUDITOR OUTPUT ---\n" + (auditor_raw or "").strip() + "\n--- END ---")
        persist_event(mem, "auditor", auditor_raw, cycle_id, level="success")

        refresh_memory_summary(mem, cycle_id, limit=140)

        # --- Ethicist (APPROVE/REJECT) ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs)

        candidate_section_eth = ""
        if parsed_files:
            primary = None
            for pf in parsed_files:
                if pf.path.strip().endswith("modules/autonomous_agent.py"):
                    primary = pf
                    break
            if not primary and parsed_files:
                primary = parsed_files[0]
            listing = "\n".join(f"- {pf.path}" for pf in parsed_files)
            candidate_section_eth = (
                "\nCANDIDATE FILES (detected):\n" + listing + "\n\n"
                "PRIMARY FILE CONTENT FOR ETHICAL REVIEW:\n"
                "```python\n" + primary.content.strip() + "\n```\n"
            )

        ethicist_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: ETHICIST\n"
            "OBJECTIVE: Assess fairness, privacy, safety, and cultural responsibility of the final plan/code.\n\n"
            "CONTEXT RULES:\n"
            "- Consider MEMORY SUMMARY and RECENT EVENTS even if generic.\n"
            "- No apologies, no meta-instructions.\n\n"
            "OUTPUT FORMAT (exact):\n"
            "- Ethical Assessment: (bullets)\n"
            "- Concerns ↔ Mitigations: (paired bullets)\n"
            "- VOTE: APPROVE or VOTE: REJECT  (last line exactly)\n"
            + candidate_section_eth
        )
        ethicist_expected = "An ethical analysis ending with the final line exactly: VOTE: APPROVE or VOTE: REJECT"
        ethicist_raw = run_single_task(ethicist, ethicist_desc, ethicist_expected)
        outputs["ethicist"] = ethicist_raw
        print("\n--- ETHICIST OUTPUT ---\n" + (ethicist_raw or "").strip() + "\n--- END ---")
        persist_event(mem, "ethicist", ethicist_raw, cycle_id, level="success")

        # ---------- Decisión Final ----------
        auditor_decision = parse_auditor_decision(auditor_raw)       # "GO", "NO-GO", "UNKNOWN"
        ethicist_vote    = parse_ethicist_vote(ethicist_raw)         # "APPROVE", "REJECT", "UNKNOWN"
        approved = (auditor_decision == "GO") and (ethicist_vote == "APPROVE")

        if approved and parsed_files:
            try:
                result = consume_creator_output(creator_raw, default_target="modules/autonomous_agent.py")
                written = ", ".join(result.get("written", []))
                persist_event(mem, "system", f"[approved_write] {written}", cycle_id, level="success")
                outputs["_rewrite_status"] = "written"
            except Exception as e:
                persist_event(mem, "system", f"[approved_write_error] {e}", cycle_id, level="error")
                outputs["_rewrite_status"] = "skipped"
        else:
            reason = f"auditor={auditor_decision}, ethicist={ethicist_vote}, files={len(parsed_files)}"
            persist_event(mem, "system", f"[proposal_rejected] {reason}", cycle_id, level="rejected")
            outputs["_rewrite_status"] = "skipped"

        # ---------- Finalize memory: rolling summary ----------
        refresh_memory_summary(mem, cycle_id, limit=160)

        return outputs

    except Exception as e:
        print("[ERROR in run_crew_stepwise()]:", str(e))
        try:
            mem.add_event({
                "title": "run_crew exception",
                "note": f"{e}",
                "tag": "system",
                "level": "error",
                "cycle_id": datetime.utcnow().isoformat(),
                "ts": datetime.utcnow().isoformat()
            })
        except Exception:
            pass
        return None
