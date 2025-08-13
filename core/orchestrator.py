# core/orchestrator.py
from __future__ import annotations

import json
import os, io, tempfile, shutil
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# -------------------------------
# Paths base del proyecto
# -------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]  # .../ia-autoreflexiva
MODULES_DIR = BASE_DIR / "modules"

# -------------------------------
# Modelos
# -------------------------------

@dataclass
class ParsedFile:
    path: str
    content: str
    language: str = "python"


# -------------------------------
# Regex y detectores de formatos
# -------------------------------
# Formato 1 (clásico): un único fence con python (sin path explícito)
PY_FENCE_RE = re.compile(r"```python\s*(.*?)```", re.DOTALL | re.IGNORECASE)
GENERIC_FENCE_RE = re.compile(r"```\s*(.*?)```", re.DOTALL)

# Formato 2: múltiples fences, cada uno con cabecera indicando la ruta
# Acepta estilos: "# file:", "# path:", "// file:", "/* file:", "<!-- file: -->", etc.
FENCE_BLOCK_RE = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL)
FILE_HEADER_RE = re.compile(
    r"""^\s*(?:[#;]|//|/\*+|<!--)\s*(?:file|filepath|path)\s*:\s*(?P<path>[^\s*<>]+)""",
    re.IGNORECASE | re.MULTILINE,
)

# Formato 3: manifiesto JSON dentro de fence
# ```json
# {"files":[{"path":"modules/x.py","language":"python","content":"..."}]}
# ```
JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


# -------------------------------
# Utilidades internas
# -------------------------------

def _safe_resolve(target: Path) -> Path:
    """
    Normaliza y garantiza que el path final quede dentro de BASE_DIR (evita path traversal).
    Lanza ValueError si intenta salir del repo.
    """
    base = BASE_DIR.resolve()
    tgt = (base / target).resolve() if not target.is_absolute() else target.resolve()
    # Compat: evitar .is_relative_to() por versiones antiguas
    base_s = str(base).replace("\\", "/")
    tgt_s = str(tgt).replace("\\", "/")
    if not tgt_s.startswith(base_s + "/") and tgt_s != base_s:
        raise ValueError(f"Ruta fuera del repositorio: {tgt}")
    return tgt


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _extract_single_fence(text: str) -> Optional[str]:
    """Devuelve el contenido del primer fence (prefiere python, si no genérico)."""
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
    Si el Creator emitió varios fences por archivo (cada uno con cabecera tipo 'file: ...'),
    los extrae y devuelve una lista de ParsedFile.
    """
    results: List[ParsedFile] = []
    if not raw:
        return results

    for m in FENCE_BLOCK_RE.finditer(raw):
        lang = (m.group("lang") or "").strip().lower() or "text"
        body = m.group("body") or ""
        header = FILE_HEADER_RE.search(body)
        if not header:
            continue
        relpath = header.group("path").strip()
        # Eliminar solo la primera cabecera del contenido
        content = FILE_HEADER_RE.sub("", body, count=1).lstrip("\n\r")
        # Normalizar lenguaje (por defecto python)
        language = "python" if lang in ("", "python", "py") else lang
        results.append(ParsedFile(path=relpath, content=content, language=language))
    return results


def _parse_json_manifest(raw: str) -> List[ParsedFile]:
    """
    Lee un bloque JSON (manifest) con múltiples archivos.
    Espera una clave 'files' con objetos {path, language?, content}.
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


def normalize_creator_output_to_dict(raw_text: str,
                                     default_target: str = "modules/autonomous_agent.py") -> Dict[str, str]:
    """
    Intenta extraer la salida del Creator en este orden:
      1) Manifiesto JSON (```json { "files": [...] } ```)
      2) Fences por archivo con cabecera '# file: path'
      3) Fence único (legacy) -> se guarda en default_target

    Devuelve siempre un dict { "ruta/relativa": "contenido" }.
    """
    files: Dict[str, str] = {}

    # 1) JSON manifest
    mf_list = _parse_json_manifest(raw_text)  # List[ParsedFile]
    if mf_list:
        return {pf.path: pf.content for pf in mf_list}

    # 2) Per-file fences con cabecera
    pf_list = _parse_per_file_fences(raw_text)  # List[ParsedFile]
    if pf_list:
        return {pf.path: pf.content for pf in pf_list}

    # 3) Fence único (legacy)
    single = _extract_single_fence(raw_text)  # str | None
    if single:
        files[default_target] = single
        return files

    # Nada parseable
    return files

# -------------------------------
# API de parseo principal
# -------------------------------

def parse_creator_outputs(raw_text: str, default_target: str = "modules/autonomous_agent.py") -> List[ParsedFile]:
    """
    Extrae TODOS los archivos de la salida del Creator soportando 3 formatos:

    1) Manifiesto JSON (preferido para multi-archivo)
       ```json
       {"files":[{"path":"modules/a.py","language":"python","content":"..."}]}
       ```
    2) Multi-fence por archivo:
       ```python
       # file: modules/autonomous_agent/core.py
       <contenido>
       ```
       (Admite '# path:' / '# filepath:' y otros estilos de comentario comunes)
    3) Bloque único “legacy” (sin path): asigna el contenido a default_target.

    Devuelve: List[ParsedFile]
    """
    if not raw_text or not raw_text.strip():
        return []

    # 1) Intentar manifest JSON
    parsed = _parse_json_manifest(raw_text)
    if parsed:
        return parsed

    # 2) Intentar múltiples fences con cabecera de archivo
    parsed = _parse_per_file_fences(raw_text)
    if parsed:
        return parsed

    # 3) Fallback: bloque único (legacy) → escribir en default_target
    single = _extract_single_fence(raw_text)
    if single:
        return [ParsedFile(path=default_target, content=single, language="python")]

    # Nada reconocido
    return []


# -------------------------------
# Escritura a disco (incremental)
# -------------------------------

def write_files(parsed_files: List[ParsedFile]) -> List[str]:
    """
    Escribe la lista de archivos parseados.
    - Crea backups con timestamp si el archivo ya existe.
    - Normaliza rutas y evita escrituras fuera del repo.
    Devuelve la lista de rutas escritas (strings).
    """
    written: List[str] = []
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    for pf in parsed_files:
        try:
            target = _safe_resolve(Path(pf.path))
        except ValueError as e:
            print(f"[orchestrator] Ruta insegura ignorada ({pf.path}): {e}")
            continue

        try:
            _ensure_parent(target)
            if target.exists():
                backup = target.with_suffix(target.suffix + f".bak-{ts}")
                try:
                    backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                    print(f"[orchestrator] Backup created: {backup}")
                except Exception as be:
                    print(f"[orchestrator] Backup failed (continuing) for {target}: {be}")

            target.write_text(pf.content, encoding="utf-8")
            print(f"[orchestrator] Wrote file: {target}")
            written.append(str(target))
        except Exception as we:
            print(f"[orchestrator] Failed writing {pf.path}: {we}")

    return written


def write_files_bundle(files: Dict[str, str], root_dir: Optional[str] = None) -> List[str]:
    """
    Escribe un conjunto de archivos (path relativo -> contenido).
    - Crea directorios intermedios.
    - Hace backup timestamp si el archivo ya existía.
    - Escribe de forma atómica (tmp + os.replace) para evitar archivos a medio escribir.
    - Normaliza fin de línea a '\n'.
    Devuelve la lista de rutas absolutas escritas.

    Params:
      files: dict {"modules/autonomous_agent/__init__.py": "...", ...}
      root_dir: base donde escribir; por defecto, cwd.
    """
    if not isinstance(files, dict):
        raise TypeError(f"write_files_bundle esperaba dict, recibió {type(files).__name__}")

    base = Path(root_dir) if root_dir else Path.cwd()
    written: List[str] = []
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    for rel, content in files.items():
        # Validación básica
        if not rel or not isinstance(rel, str):
            raise ValueError(f"Ruta inválida en bundle: {rel!r}")
        if not isinstance(content, str):
            raise TypeError(f"Contenido para {rel} debe ser str, no {type(content).__name__}")

        target = (base / rel).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        # Backup si existe
        if target.exists():
            backup = target.with_suffix(target.suffix + f".bak-{ts}")
            try:
                backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"[write_files_bundle] Backup creado: {backup}")
            except Exception as e:
                print(f"[write_files_bundle] Backup falló (continuo): {e}")

        # Escritura atómica: a tmp en el mismo dir + replace
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
        try:
            # Normaliza EOL a '\n' y asegura UTF-8
            normalized = content.replace("\r\n", "\n").replace("\r", "\n")
            with io.open(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(normalized)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp_path, target)  # atómico en la mayoría de FS
        except Exception:
            # Limpieza del tmp si algo falla
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise
        print(f"[write_files_bundle] Escrito: {target}")
        written.append(str(target))
    return written


# -------------------------------
# API de consumo para crew_config
# -------------------------------

def consume_creator_output(raw_text: str, default_target: str = "modules/autonomous_agent.py") -> Dict[str, List[str]]:
    """
    Parsea la salida del Creator y escribe los archivos (incremental).

    Devuelve un dict con:
      {
        "detected": [lista de rutas relativas detectadas],
        "written":  [lista de rutas absolutas escritas]
      }
    """
    parsed = parse_creator_outputs(raw_text, default_target=default_target)
    detected = [pf.path for pf in parsed]
    written = write_files(parsed) if parsed else []
    return {"detected": detected, "written": written}


# -------------------------------
# Resumen y tabla de resultados
# -------------------------------

def _first_line(s: str) -> str:
    if not s:
        return ""
    return s.strip().splitlines()[0][:160]


def summarize_results(outputs: Dict[str, str]) -> Dict[str, str]:
    """
    Hace un resumen puntual (primera línea) de los 5 agentes.
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
    return "\n".join(f"{k.ljust(max_key)} : {v}" for k, v in rows)
