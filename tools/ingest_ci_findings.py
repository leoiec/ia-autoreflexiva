# file: tools/ingest_ci_findings.py
"""
Ingesta salidas de CI (flake8, mypy, pytest, pip) y crea/actualiza ítems en backlog.jsonl.

Uso típico (en CI):
  python -m tools.ingest_ci_findings --logs-dir repo/.ci_logs --owner creator

Convenciones de archivo dentro de --logs-dir:
  - flake8.txt
  - mypy.txt
  - pytest.txt
  - pip.txt
Si no existen, se omiten silenciosamente.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from tools.backlog_lib import create_or_reopen

# --- Parsers ---

_FLK = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?::\d+)?:\s+(?P<rule>[A-Z]{1,3}\d{0,3})\s+(?P<msg>.+)$")
_MPY = re.compile(r"^(?P<file>.+?):(?P<line>\d+):\s+error:\s+(?P<msg>.+?)(?:\s+\[(?P<rule>[a-z0-9\-_]+)\])?$", re.I)

def parse_lines(lines: Iterable[str], kind: str, source: str) -> List[Dict]:
    items: List[Dict] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if source == "flake8":
            m = _FLK.match(line)
            if not m:
                continue
            d = m.groupdict()
            title = f"{d['rule']} {d['msg']}"
            items.append(dict(kind="lint", title=title, owner="creator",
                              file=d["file"], line=int(d["line"]), rule=d["rule"], source=source))
        elif source == "mypy":
            m = _MPY.match(line)
            if not m:
                continue
            d = m.groupdict()
            rule = d.get("rule") or "mypy"
            title = f"{rule} {d['msg']}"
            items.append(dict(kind="typecheck", title=title, owner="creator",
                              file=d["file"], line=int(d["line"]), rule=rule, source=source))
        elif source == "pip":
            # heurística: cualquier línea con "conflict" o "depends on" la registramos como deps
            if ("conflict" in line.lower()) or ("depends on" in line.lower()):
                items.append(dict(kind="deps", title=line[:200], owner="architect",
                                  file=None, line=None, rule="pip", source=source))
        elif source == "pytest":
            # Simplificado: marca una tarea general por fallo
            if line.startswith("FAILED ") or line.startswith("ERROR ") or line.startswith("== ") and "failed" in line.lower():
                items.append(dict(kind="test", title=line[:200], owner="creator",
                                  file=None, line=None, rule="pytest", source=source))
    return items

def load_file(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", default=os.path.join("repo", ".ci_logs"))
    ap.add_argument("--owner", default=None, help="Override owner (opcional)")
    ap.add_argument("--backlog", default=None, help="Ruta a backlog.jsonl")
    args = ap.parse_args()

    logs_dir = args.logs_dir
    sources = [
        ("flake8", os.path.join(logs_dir, "flake8.txt")),
        ("mypy",   os.path.join(logs_dir, "mypy.txt")),
        ("pytest", os.path.join(logs_dir, "pytest.txt")),
        ("pip",    os.path.join(logs_dir, "pip.txt")),
    ]

    total = 0
    for src, path in sources:
        lines = load_file(path)
        if not lines:
            continue
        items = parse_lines(lines, kind=src, source=src)
        for it in items:
            if args.owner:
                it["owner"] = args.owner
            create_or_reopen(
                kind=it["kind"],
                title=it["title"],
                owner=it["owner"],
                file=it.get("file"),
                line=it.get("line"),
                rule=it.get("rule"),
                source=it["source"],
                note=None,
                path=args.backlog or None,
            )
            total += 1

    print(f"[ingest_ci_findings] created_or_reopened={total}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
