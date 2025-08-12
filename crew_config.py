from __future__ import annotations

import os
import re, json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from crewai import Task, Crew
from crewai.process import Process

from agents.creator import CreatorAgent
from agents.auditor import AuditorAgent
from agents.ethicist import EthicistAgent
from agents.architect import ArchitectAgent
from agents.revolutionary import RevolutionaryAgent

# Orquestador de parches incrementales
from core.orchestrator import (
    parse_creator_outputs,          # Inspección sin escribir
    consume_creator_output,         # Escritura efectiva aprobada
    summarize_results,
    format_results_table,
)

from core.memory.memory_store import MemoryStore
from core.memory.summarizer import summarize_events, build_local_llm


# ---------- Utilidades para cargar TODO el módulo autónomo ----------

def _autonomous_package_root() -> str:
    """Ruta base del paquete, si existe."""
    return os.path.join("modules", "autonomous_agent")

def _autonomous_legacy_path() -> str:
    """Ruta del archivo legacy (single-file)."""
    return os.path.join("modules", "autonomous_agent.py")

def list_autonomous_files() -> List[str]:
    """
    Devuelve la lista de archivos a incluir en el prompt:
    - Si existe el paquete: todos los .py dentro de modules/autonomous_agent/ (recursivo),
      ordenados por ruta (priorizando __init__.py y core.py al frente de la lista).
    - Si no existe el paquete pero existe el legacy: devuelve [legacy].
    - Si no hay nada: lista vacía.
    """
    pkg = _autonomous_package_root()
    if os.path.isdir(pkg):
        paths = []
        for root, _, files in os.walk(pkg):
            for fn in files:
                if fn.endswith(".py"):
                    paths.append(os.path.join(root, fn))
        # Orden estable: __init__.py primero, luego core.py, luego el resto alfabético
        def _key(p: str) -> Tuple[int, int, str]:
            base = os.path.basename(p).lower()
            is_init = 0 if base == "__init__.py" else 1
            is_core = 0 if base == "core.py" else 1
            return (is_init, is_core, p.lower())
        paths.sort(key=_key)
        return paths
    # fallback a legacy
    legacy = _autonomous_legacy_path()
    return [legacy] if os.path.isfile(legacy) else []

def load_files_content(paths: List[str]) -> List[Tuple[str, str]]:
    """Carga contenido de cada archivo (path, content). Ignora faltantes sin romper flujo."""
    out: List[Tuple[str, str]] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.append((p, f.read()))
        except FileNotFoundError:
            out.append((p, f"# ERROR: File {p} not found."))
        except Exception as e:
            out.append((p, f"# ERROR reading {p}: {e}"))
    return out

def build_modules_prompt(files: List[Tuple[str, str]]) -> str:
    """
    Construye un prompt multi-archivo. Cada archivo se separa con cabecera:
      FILE: <path>
      --- BEGIN CODE ---
      <content>
      --- END CODE ---
    """
    if not files:
        return "NO MODULES FOUND."
    chunks = []
    for path, content in files:
        chunks.append(
            f"FILE: {path}\n--- BEGIN CODE ---\n{content}\n--- END CODE ---"
        )
    header = "TARGET MODULE: modules/autonomous_agent (package) OR modules/autonomous_agent.py (legacy)\n"
    return header + "\n\n".join(chunks)

def preferred_default_target(files: List[Tuple[str, str]]) -> str:
    """
    El archivo objetivo por defecto para flujos que requieren uno:
    - Si hay paquete: preferimos modules/autonomous_agent/__init__.py
      (es la cara pública) y si no existe, core.py; sino el primero.
    - Si es legacy: ese mismo archivo.
    """
    if not files:
        return _autonomous_legacy_path()
    paths = [p for p, _ in files]
    pkg_init = os.path.join(_autonomous_package_root(), "__init__.py")
    core_py  = os.path.join(_autonomous_package_root(), "core.py")
    if pkg_init in paths:
        return pkg_init
    if core_py in paths:
        return core_py
    return paths[0]


# ---------- Helpers previos (sin cambios sustanciales) ----------

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


# --- Single-task runner + retry para Creator ---

def run_single_task(agent, description: str, expected_output: str) -> str:
    task = Task(description=description, expected_output=expected_output, agent=agent)
    c = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=True)
    out = c.kickoff()
    return out.tasks_output[0].raw if out and out.tasks_output else ""

# --- Strict validation for Creator outputs (formats A/B/C) ---

# fences ```...``` (with or without language tag)
_FENCE_RE = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)

# Header "# file: relative/path"
_FILE_HEADER_RE = re.compile(
    r"""^\s*(?:[#;]|//|/\*+|<!--)\s*file\s*:\s*(?P<path>[^\s*<>]+)\s*""",
    re.IGNORECASE | re.MULTILINE,
)

# Legacy allowed path for format A
_SINGLE_LEGACY_PATH = "modules/autonomous_agent.py"

def _looks_like_single_file(raw_text: str) -> bool:
    """
    Format A: a single python fence containing the legacy file content.
    No '# file:' header required; entire body is treated as the file content.
    """
    if not raw_text:
        return False
    fences = list(_FENCE_RE.finditer(raw_text))
    if len(fences) != 1:
        return False
    lang = (fences[0].group("lang") or "").strip().lower()
    body = (fences[0].group("body") or "").strip()
    if lang and "python" not in lang:
        return False
    return len(body) > 50  # minimum threshold to avoid empty replies

def _looks_like_multi_file(raw_text: str) -> bool:
    """
    Format B: one or more fences; each fence starts with '# file: path'.
    Paths must be relative, non-empty, and unique.
    """
    fences = list(_FENCE_RE.finditer(raw_text or ""))
    if not fences:
        return False
    seen = set()
    for m in fences:
        body = m.group("body") or ""
        h = _FILE_HEADER_RE.search(body)
        if not h:
            return False
        path = h.group("path").strip()
        if not path or path.startswith(("/", "\\")):
            return False
        if path in seen:
            return False
        seen.add(path)
        body_clean = _FILE_HEADER_RE.sub("", body, count=1).strip()
        if len(body_clean) < 5:
            return False
    return True

def _looks_like_json_manifest(raw_text: str) -> bool:
    """
    Format C: a single JSON block containing {"files":[{"path":...,"content":...}]}
    """
    fences = list(_FENCE_RE.finditer(raw_text or ""))
    if len(fences) != 1:
        return False
    lang = (fences[0].group("lang") or "").strip().lower()
    body = fences[0].group("body") or ""
    try:
        obj = json.loads(body)
    except Exception:
        return False
    if not isinstance(obj, dict) or "files" not in obj or not isinstance(obj["files"], list):
        return False
    if not obj["files"]:
        return False
    seen = set()
    for f in obj["files"]:
        if not isinstance(f, dict):
            return False
        path = f.get("path", "").strip()
        content = f.get("content", "")
        if not path or path.startswith(("/", "\\")):
            return False
        if path in seen:
            return False
        seen.add(path)
        if not isinstance(content, str) or len(content) < 1:
            return False
    return True

def _detect_creator_format(raw_text: str) -> str | None:
    if _looks_like_multi_file(raw_text):
        return "multi"
    if _looks_like_json_manifest(raw_text):
        return "manifest"
    if _looks_like_single_file(raw_text):
        return "single"
    return None

# --- Retry instructions in English ---
RETRY_CREATOR_INSTRUCTIONS = """
FORMAT CORRECTION:
Your previous output did not match the required rules. You must use EXACTLY ONE of the formats below.

A) Single file (legacy)
   - EXACTLY ONE Python code fence with the FULL content of: modules/autonomous_agent.py
   - NO text outside the code fence.
   - Example:
     ```python
     # (full content of modules/autonomous_agent.py)
     ```

B) Multi-file (per file)
   - FOR EACH file, output ONE code fence (preferably with a language tag).
   - The FIRST line inside each fence MUST declare the relative path:
       # file: modules/autonomous_agent/__init__.py
       # file: modules/autonomous_agent/core.py
       # file: modules/autonomous_agent/consent.py
   - Then the COMPLETE content of that file.
   - NO prose outside the code fences.
   - Example:
     ```python
     # file: modules/autonomous_agent/__init__.py
     # (content)
     ```
     ```python
     # file: modules/autonomous_agent/core.py
     # (content)
     ```

C) JSON manifest
   - A SINGLE JSON block with this structure:
     ```json
     {
       "files": [
         {"path": "modules/autonomous_agent/__init__.py", "language": "python", "content": "..."},
         {"path": "modules/autonomous_agent/core.py",     "language": "python", "content": "..."}
       ]
     }
     ```
   - 'content' must contain the FULL content of each file (not diffs).

Re-submit NOW using one (and only one) of the formats A, B, or C. Do not include text outside the blocks.
"""

def retry_creator_if_needed(raw_text: str, creator_agent, base_description: str, expected_output: str) -> str:
    """
    Accepts current output if it matches A/B/C.
    If not, retries with detailed instructions and returns the new output.
    """
    format_kind = _detect_creator_format(raw_text or "")
    if format_kind is not None:
        return raw_text  # valid

    fixed_desc = base_description + "\n\n" + RETRY_CREATOR_INSTRUCTIONS
    task = Task(description=fixed_desc, expected_output=expected_output, agent=creator_agent)
    c = Crew(agents=[creator_agent], tasks=[task], process=Process.sequential, verbose=True)
    out = c.kickoff()
    new_raw = out.tasks_output[0].raw if out and out.tasks_output else ""
    if _detect_creator_format(new_raw or "") is None:
        return new_raw  # still invalid
    return new_raw


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


# ---------- Orquestación paso a paso (con multi-archivo) ----------

def run_crew():
    """
    Ejecución por etapas con refresco de memoria e inyección de digest por ciclo.
    Flujo: Architect → Revolutionary → Creator → Auditor → Ethicist.
    - Architect & Creator reciben TODO el contenido del módulo autónomo:
      * Si hay paquete: todos los .py de modules/autonomous_agent/**
      * Si no, el archivo legacy modules/autonomous_agent.py
    - Creator puede emitir:
        A) archivo único (legacy),
        B) múltiples archivos con '# file: ...' en cada fence,
        C) manifiesto JSON {"files":[...]}.
      La escritura efectiva ocurre SOLO si Auditor=GO y Ethicist=APPROVE.
    """
    mem = MemoryStore()
    try:
        # Purga inicial: fuera errores y rechazos + frases de ruido
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

        # --- NUEVO: cargar TODOS los archivos del módulo autónomo ---
        module_paths = list_autonomous_files()
        files = load_files_content(module_paths)
        module_prompt = build_modules_prompt(files)
        default_target = preferred_default_target(files)

        # Instanciar agentes
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

        # --- Creator (FORMATOS A/B/C + RETRY) ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs, max_chars=1200)
        creator_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: CREATOR\n"
            "OBJECTIVE: Integrate Architect+Revolutionary into a coherent implementation for the TARGET PACKAGE.\n\n"
            "IMPORTANT OUTPUT FORMAT RULES — FOLLOW EXACTLY:\n"
            "• Emit a MULTI-FILE BUNDLE using one fenced code block PER FILE.\n"
            "• Each block MUST start with a header line declaring the path:\n"
            "    # file: modules/autonomous_agent/__init__.py\n"
            "    # file: modules/autonomous_agent/core.py\n"
            "    # file: modules/autonomous_agent/consent.py\n"
            "  (Usa la ruta relativa exacta dentro del repo.)\n"
            "• No incluyas texto fuera de los fences salvo, si quieres, una lista breve de archivos generados.\n"
            "• Incluye el CONTENIDO COMPLETO de cada archivo (no diffs).\n\n"
            + module_prompt  # incluye el código actual como contexto
        )
        creator_expected = (
            "A multi-file code bundle with one fenced block per file, each starting "
            "with '# file: <relative/path>'. No partial files; full contents only."
        )
        creator_raw = run_single_task(creator, creator_desc, creator_expected)
        creator_raw = retry_creator_if_needed(creator_raw, creator, creator_desc, creator_expected)
        outputs["creator"] = creator_raw
        print("\n--- CREATOR OUTPUT ---\n" + (creator_raw or "").strip() + "\n--- END ---")

        # EXTRAER TODOS LOS ARCHIVOS DEL BUNDLE
        pending_files = parse_creator_outputs(creator_raw)
        if pending_files:
            persist_event(mem, "creator", f"[code_generated] {', '.join(sorted(pending_files.keys()))}", cycle_id, level="success")
        else:
            persist_event(mem, "creator", "[no_files_detected_in_bundle]", cycle_id, level="error")
            print("[creator] No se detectaron archivos válidos en la salida; no se considerará propuesta.")

        refresh_memory_summary(mem, cycle_id, limit=120)

        # --- AUDITOR (GO/NO-GO) ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs)

        bundle_section = ""
        if pending_files:
            for pth, src in pending_files.items():
                bundle_section += f"\nFILE: {pth}\n```text\n{src}\n```\n"

        auditor_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: AUDITOR\n"
            "OBJECTIVE: Review the Creator’s multi-file bundle for technical soundness, coherence, performance and safety.\n\n"
            "OUTPUT FORMAT (exact):\n"
            "- Findings: (bullets)\n"
            "- Risks & Mitigations: (paired bullets)\n"
            "- Test Strategy: (bullets)\n"
            "- DECISION: GO or DECISION: NO-GO  (last line exactly)\n"
            + bundle_section
        )
        auditor_expected = "A structured audit report ending with exactly: DECISION: GO or DECISION: NO-GO"
        auditor_raw = run_single_task(auditor, auditor_desc, auditor_expected)
        outputs["auditor"] = auditor_raw
        print("\n--- AUDITOR OUTPUT ---\n" + (auditor_raw or "").strip() + "\n--- END ---")
        persist_event(mem, "auditor", auditor_raw, cycle_id, level="success")

        refresh_memory_summary(mem, cycle_id, limit=140)

        # --- ETHICIST (APPROVE/REJECT) ---
        system_ctx = build_system_context(mem)
        digest = short_cycle_digest(outputs)

        ethicist_desc = (
            system_ctx + "\n"
            f"CYCLE DIGEST (previous outputs in this run):\n{digest}\n\n"
            "ROLE: ETHICIST\n"
            "OBJECTIVE: Assess fairness, privacy, safety, and cultural responsibility of the final multi-file plan/code.\n\n"
            "OUTPUT FORMAT (exact):\n"
            "- Ethical Assessment: (bullets)\n"
            "- Concerns ↔ Mitigations: (paired bullets)\n"
            "- VOTE: APPROVE or VOTE: REJECT  (last line exactly)\n"
            + bundle_section
        )
        ethicist_expected = "An ethical analysis ending exactly with: VOTE: APPROVE or VOTE: REJECT"
        ethicist_raw = run_single_task(ethicist, ethicist_desc, ethicist_expected)
        outputs["ethicist"] = ethicist_raw
        print("\n--- ETHICIST OUTPUT ---\n" + (ethicist_raw or "").strip() + "\n--- END ---")
        persist_event(mem, "ethicist", ethicist_raw, cycle_id, level="success")

        # ---------- Decisión Final ----------
        auditor_decision = parse_auditor_decision(auditor_raw)       # "GO", "NO-GO", "UNKNOWN"
        ethicist_vote    = parse_ethicist_vote(ethicist_raw)         # "APPROVE", "REJECT", "UNKNOWN"
        approved = (auditor_decision == "GO") and (ethicist_vote == "APPROVE")

        if approved and pending_files:
            try:
                written_paths = write_files_bundle(pending_files, root_dir=".")
                persist_event(mem, "system", f"[approved_write] {len(written_paths)} files", cycle_id, level="success")
                outputs["_rewrite_status"] = "written"
            except Exception as e:
                persist_event(mem, "system", f"[approved_write_error] {e}", cycle_id, level="error")
                outputs["_rewrite_status"] = "skipped"
        else:
            reason = f"auditor={auditor_decision}, ethicist={ethicist_vote}, files={len(pending_files) if pending_files else 0}"
            persist_event(mem, "system", f"[proposal_rejected] {reason}", cycle_id, level="rejected")
            outputs["_rewrite_status"] = "skipped"

        # (Opcional útil) imprimir un mini resumen en consola
        try:
            from core.orchestrator import summarize_results, format_results_table
            print("\n=== Cycle Summary ===")
            print(format_results_table(summarize_results(outputs)))
        except Exception:
            pass

        # IMPORTANTE: devolver outputs para uso del caller
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
