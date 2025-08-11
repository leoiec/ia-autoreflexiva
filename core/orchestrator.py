# core/orchestrator.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# Rutas base
BASE_DIR = Path(__file__).resolve().parents[1]
MODULES_DIR = BASE_DIR / "modules"

# -------------------------------
# Parsing de salidas del Creator
# -------------------------------

# Formato 1 (clásico): un único fence con python (sin path explícito)
PY_FENCE_RE = re.compile(r"```python\s*(.*?)```", re.DOTALL | re.IGNORECASE)
GENERIC_FENCE_RE = re.compile(r"```\s*(.*?)```", re.DOTALL)

# Formato 2: fence por archivo con cabecera en la primera(s) línea(s)
# Ejemplos aceptados:
#   # file: modules/foo.py
#   # path: modules/foo.py
#   # filepath: modules/foo.py
#   # FILE: modules/foo.py
HEADER_PATH_RE = re.compile(
    r"^\s*#\s*(?:file|filepath|path)\s*:\s*(?P<path>[^\n\r]+)$",
    re.IGNORECASE | re.MULTILINE,
)

# Formato 3: manifest JSON con múltiples archivos
# ```json
# {"files":[{"path":"modules/x.py","language":"python","content":"..."}]}
# ```
JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


@dataclass
class ParsedFile:
    path: str
    content: str
    language: str = "python"


def _extract_single_fence(text: str) -> Optional[str]:
    """Devuelve el contenido del primer fence (python preferido, si no genérico)."""
    if not text:
        return None
    m = PY_FENCE_RE.search(text)
    if m:
        return m.group(1)
    m2 = GENERIC_FENCE_RE.search(text)
    if m2:
        return m2.group(1)
    return None


def _parse_per_file_fences(raw: str) -> List[ParsedFile]:
    """
    Si el Creator emitió varios fences por archivo (cada uno con cabecera '# file: ...'),
    los extrae y devuelve una lista de ParsedFile.
    """
    results: List[ParsedFile] = []
    if not raw:
        return results

    # Encontrar TODOS los fences (python o genéricos)
    fences = re.findall(r"```(?:python)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    for fence in fences:
        header = HEADER_PATH_RE.search(fence)
        if header:
            relpath = header.group("path").strip()
            # Remover la(s) línea(s) de cabecera del contenido
            content = HEADER_PATH_RE.sub("", fence, count=1).lstrip("\n\r")
            results.append(ParsedFile(path=relpath, content=content, language="python"))
    return results


def _parse_json_manifest(raw: str) -> List[ParsedFile]:
    """
    Lee un bloque JSON (manifest) con múltiples archivos.
    """
    results: List[ParsedFile] = []
    if not raw:
        return results

    m = JSON_FENCE_RE.search(raw)
    if not m:
        return results

    try:
        data = json.loads(m.group(1))
    except Exception:
        return results

    files = data.get("files") or []
    for f in files:
        path = str(f.get("path", "")).strip()
        content = f.get("content", "")
        lang = (f.get("language") or "python").lower()
        if path and isinstance(content, str):
            results.append(ParsedFile(path=path, content=content, language=lang))
    return results


def parse_creator_outputs(raw_text: str, default_target: str = "modules/autonomous_agent.py") -> List[ParsedFile]:
    """
    Soporta tres estilos:
      1) Único fence (se asume default_target)
      2) Varios fences con cabecera '# file: ...'
      3) Manifest JSON con 'files'
    Prioridad: (3) JSON > (2) por-archivo > (1) único fence
    """
    # 3) Manifest JSON
    files = _parse_json_manifest(raw_text)
    if files:
        return files

    # 2) Por-archivo con cabecera
    files = _parse_per_file_fences(raw_text)
    if files:
        return files

    # 1) Único fence (legacy)
    solo = _extract_single_fence(raw_text)
    if solo:
        return [ParsedFile(path=default_target, content=solo, language="python")]

    return []


# -------------------------------
# Escritura a disco (incremental)
# -------------------------------

def _ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def write_files(parsed_files: List[ParsedFile]) -> List[str]:
    """
    Escribe la lista de archivos parseados. Crea backups timestamp si el archivo ya existe.
    Devuelve la lista de paths escritos.
    """
    written: List[str] = []
    for pf in parsed_files:
        # Normaliza path a la carpeta modules si es relativo
        target = Path(pf.path)
        if not target.is_absolute():
            target = BASE_DIR / target

        try:
            _ensure_parent(target)
            if target.exists():
                ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                backup = target.with_suffix(target.suffix + f".bak-{ts}")
                try:
                    backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                    print(f"[orchestrator] Backup created: {backup}")
                except Exception as e:
                    print(f"[orchestrator] Backup failed (continuing): {e}")

            target.write_text(pf.content, encoding="utf-8")
            print(f"[orchestrator] Wrote file: {target}")
            written.append(str(target))
        except Exception as e:
            print(f"[orchestrator] Failed writing {target}: {e}")
    return written


# API principal que usa crew_config.py
def consume_creator_output(raw_text: str, default_target: str = "modules/autonomous_agent.py") -> Dict[str, List[str]]:
    """
    Parsea la salida del Creator y escribe los archivos (incremental).
    Devuelve un dict con:
      {
        "detected": [lista de paths detectados],
        "written": [lista de paths escritos]
      }
    """
    parsed = parse_creator_outputs(raw_text, default_target=default_target)
    detected = [p.path for p in parsed]
    written = write_files(parsed) if parsed else []
    return {"detected": detected, "written": written}


# ------------------------------------
# Resumen y tabla de resultados (UI)
# ------------------------------------

def _first_line(s: str) -> str:
    if not s:
        return ""
    return s.strip().splitlines()[0][:160]


def summarize_results(outputs: Dict[str, str]) -> Dict[str, str]:
    """
    Hace un resumen puntual de los 5 agentes, tomando la primera línea.
    """
    return {
        "architect": _first_line(outputs.get("architect", "")),
        "revolutionary": _first_line(outputs.get("revolutionary", "")),
        "creator": _first_line(outputs.get("creator", "")),
        "auditor": _first_line(outputs.get("auditor", "")),
        "ethicist": _first_line(outputs.get("ethicist", "")),
        "_rewrite_status": outputs.get("_rewrite_status", "unknown"),
    }


def format_results_table(summary: Dict[str, str]) -> str:
    """
    Devuelve una tabla en texto plano con el estado del ciclo.
    """
    rows = [
        ("Architect", summary.get("architect", "")),
        ("Revolutionary", summary.get("revolutionary", "")),
        ("Creator", summary.get("creator", "")),
        ("Auditor", summary.get("auditor", "")),
        ("Ethicist", summary.get("ethicist", "")),
        ("_rewrite_status", summary.get("_rewrite_status", "")),
    ]
    max_key = max(len(k) for k, _ in rows)
    out_lines = []
    for k, v in rows:
        out_lines.append(f"{k.ljust(max_key)} : {v}")
    return "\n".join(out_lines)
