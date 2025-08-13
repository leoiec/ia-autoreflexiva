from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from pydantic import BaseModel, Field, validator
except Exception:
    # Fallback liviano si pydantic no está disponible: validación mínima
    BaseModel = object  # type: ignore
    Field = lambda default=None, **kwargs: default  # type: ignore
    def validator(*args, **kwargs):  # type: ignore
        def deco(f):
            return f
        return deco


SAFE_OPS = {"upsert", "update", "delete"}
SAFE_LANGS = {"python", "text", "markdown", "json", "yaml", "toml"}


class PatchChange(BaseModel):
    path: str = Field(..., description="Relative path inside the repo")
    op: str = Field(..., description="one of: upsert | update | delete")
    language: Optional[str] = Field(None, description="language hint (optional)")
    content: Optional[str] = Field(None, description="full file content for upsert/update")
    diff: Optional[str] = Field(None, description="unified diff for update")
    note: Optional[str] = Field(None, description="short explanation")

    @validator("op")
    def _op_valid(cls, v: str) -> str:
        if v not in SAFE_OPS:
            raise ValueError(f"Invalid op '{v}'. Allowed: {sorted(SAFE_OPS)}")
        return v

    @validator("language", always=True)
    def _lang_hint(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v not in SAFE_LANGS:
            # no bloquea, solo normaliza
            return v.lower()
        return v

    @validator("path")
    def _path_secure(cls, v: str) -> str:
        # No rutas absolutas ni escaparse del repo con ..
        if os.path.isabs(v):
            raise ValueError("Absolute paths are not allowed")
        if ".." in Path(v).parts:
            raise ValueError("Path traversal '..' is not allowed")
        if v.strip() == "":
            raise ValueError("Empty path")
        return v


class PatchSet(BaseModel):
    plan: Optional[str] = Field(None, description="short natural-language summary")
    changes: List[PatchChange] = Field(..., description="list of file changes")

    @validator("changes")
    def _non_empty(cls, v: List[PatchChange]) -> List[PatchChange]:
        if not v:
            raise ValueError("changes must not be empty")
        return v


@dataclass
class ApplyResult:
    applied: List[Dict[str, Any]]
    skipped: List[Dict[str, Any]]
    errors: List[Dict[str, Any]]


# ----------------------------
# Core helpers
# ----------------------------

def _ensure_repo_root(repo_root: Union[str, Path]) -> Path:
    root = Path(repo_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Repo root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Repo root is not a directory: {root}")
    return root


def _safe_target(root: Path, rel_path: str) -> Path:
    target = (root / rel_path).resolve()
    # Debe quedar dentro de root
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Illegal target outside repo: {rel_path}")
    return target


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")




# ----------------------------
# Public API
# ----------------------------

def apply_patch_set(
    patch_set: Union[PatchSet, Dict[str, Any], str],
    repo_root: Union[str, Path] = ".",
    dry_run: bool = False,
    protected_paths: Optional[List[str]] = None,
) -> ApplyResult:
    """
    Aplica un PatchSet (contrato patch_set_v1).

    Acepta:
      - PatchSet ya validado (Pydantic)
      - dict compatible
      - str JSON

    Operaciones:
      - upsert: crea/reemplaza con 'content' completo
      - update: si trae 'content', reemplaza; si trae 'diff', actualmente rechazamos por fiabilidad
      - delete: elimina archivo si existe

    Seguridad:
      - Paths relativos, sin '..'
      - Bloquea rutas en protected_paths
      - Nunca crea archivos fuera de repo_root

    Retorna:
      - ApplyResult con applied/skipped/errors (por archivo)
    """
    root = _ensure_repo_root(repo_root)
    protected_paths = protected_paths or []

    # Normaliza input
    if isinstance(patch_set, str):
        data = json.loads(patch_set)
    elif isinstance(patch_set, dict):
        data = patch_set
    else:
        data = json.loads(patch_set.json())  # PatchSet -> dict

    # Valida con Pydantic si está disponible
    try:
        ps = PatchSet(**data)
    except Exception as e:
        # Fallback sin pydantic: validación mínima
        if not isinstance(data, dict) or "changes" not in data or not isinstance(data["changes"], list):
            raise ValueError(f"Invalid patch_set payload: {e}")
        # Construye cambios minimalmente
        try:
            ps = PatchSet(**data)  # reintento (si pydantic está)
        except Exception:
            # Validación manual super básica
            class _MiniChange:
                def __init__(self, d: Dict[str, Any]):
                    self.path = d["path"]
                    self.op = d["op"]
                    self.language = d.get("language")
                    self.content = d.get("content")
                    self.diff = d.get("diff")
                    self.note = d.get("note")

            ps = PatchSet(
                plan=data.get("plan"),
                changes=[PatchChange(**c) if isinstance(PatchChange, type) else _MiniChange(c) for c in data["changes"]]  # type: ignore
            )

    results_applied: List[Dict[str, Any]] = []
    results_skipped: List[Dict[str, Any]] = []
    results_errors: List[Dict[str, Any]] = []

    for ch in ps.changes:
        # Si estamos en fallback sin Pydantic, ch podría no ser instancia de PatchChange real
        ch_path = getattr(ch, "path", None)
        ch_op = getattr(ch, "op", None)
        ch_content = getattr(ch, "content", None)
        ch_diff = getattr(ch, "diff", None)
        ch_note = getattr(ch, "note", None)

        try:
            if ch_path is None or ch_op is None:
                raise ValueError("Missing 'path' or 'op' in change")

            target = _safe_target(root, ch_path)

            # Protegidos
            for p in protected_paths:
                pp = _safe_target(root, p)
                if str(target) == str(pp) or str(target).startswith(str(pp) + os.sep):
                    results_skipped.append({
                        "path": ch_path,
                        "op": ch_op,
                        "reason": "protected_path",
                    })
                    raise PermissionError(f"Attempted to modify protected path: {ch_path}")

            if ch_op == "delete":
                if dry_run:
                    results_applied.append({"path": ch_path, "op": ch_op, "dry_run": True})
                else:
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    results_applied.append({"path": ch_path, "op": ch_op, "status": "deleted"})
                continue

            if ch_op in {"upsert", "update"}:
                if ch_content is not None:
                    if dry_run:
                        results_applied.append({"path": ch_path, "op": ch_op, "dry_run": True, "mode": "content"})
                    else:
                        _write_text(target, ch_content)
                        results_applied.append({"path": ch_path, "op": ch_op, "status": "written", "mode": "content"})
                    continue

                if ch_diff is not None:
                    # Por fiabilidad, rechazamos diffs por ahora (podemos soportarlos luego)
                    raise ValueError("diff-based updates are not supported yet. Provide full 'content'.")

                # Si ni content ni diff:
                raise ValueError("update/upsert requires 'content' or 'diff'")

            # Op inválida (debería estar validada)
            raise ValueError(f"Unsupported op: {ch_op}")

        except PermissionError as pe:
            results_skipped.append({"path": ch_path, "op": ch_op, "error": str(pe)})
        except Exception as e:
            results_errors.append({
                "path": ch_path,
                "op": ch_op,
                "error": str(e),
                "note": ch_note
            })

    return ApplyResult(applied=results_applied, skipped=results_skipped, errors=results_errors)


# Utilidad pequeña: crear un patch_set a partir de archivos (para tests/herramientas)
def build_patch_set_from_files(plan: str, pairs: List[Tuple[str, str]]) -> Dict[str, Any]:
    """
    pairs: list of (path, content) to upsert.
    """
    return {
        "plan": plan,
        "changes": [
            {"path": p, "op": "upsert", "language": _guess_lang(p), "content": c}
            for (p, c) in pairs
        ]
    }


def _guess_lang(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".txt": "text",
    }.get(ext, "text")
