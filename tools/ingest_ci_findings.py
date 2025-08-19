#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional

# ---- helpers --------------------------------------------------------------

def _read(path: Optional[str]) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def _read_status(path: Optional[str]) -> int:
    try:
        txt = _read(path).strip()
        return int(txt) if txt else 0
    except Exception:
        return 0

def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _normalize_lines(blob: str) -> List[str]:
    return [ln.rstrip("\n") for ln in (blob or "").splitlines()]

# ---- parsers --------------------------------------------------------------

def parse_flake8(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # form: path:line:col: CODE message
    for ln in _normalize_lines(text):
        if not ln or ":" not in ln:
            continue
        parts = ln.split(":", 3)
        if len(parts) < 4:
            continue
        path, line, col, rest = parts
        rest = rest.strip()
        code, msg = (rest.split(" ", 1) + [""])[:2]
        out.append({
            "tool": "flake8",
            "path": path.strip(),
            "line": int(line or 0),
            "col": int(col or 0),
            "code": code.strip(),
            "message": msg.strip(),
        })
    return out

def parse_mypy(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # form: path:line: error: Message  [code]
    for ln in _normalize_lines(text):
        if not ln or ": error:" not in ln:
            continue
        try:
            path, line, rest = ln.split(":", 2)
            after = rest.split("error:", 1)[1].strip()
            msg, code = (after.rsplit("[", 1) + [""])[:2]
            code = code.rstrip("]") if code else "mypy"
            out.append({
                "tool": "mypy",
                "path": path.strip(),
                "line": int(line or 0),
                "code": code.strip(),
                "message": msg.strip(),
            })
        except Exception:
            continue
    return out

def parse_pytest(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # simple: registrar resumen de fallos
    for ln in _normalize_lines(text):
        if ln.startswith("FAILED ") or "== FAILURES ==" in ln or "E   " in ln:
            out.append({
                "tool": "pytest",
                "message": ln.strip(),
            })
    return out

# ---- main ------------------------------------------------------------------

def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flake8", default=None)
    ap.add_argument("--flake8-status", default=None)
    ap.add_argument("--mypy", default=None)
    ap.add_argument("--mypy-status", default=None)
    ap.add_argument("--pytest", default=None)
    ap.add_argument("--pytest-status", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    items: List[Dict[str, Any]] = []
    # flake8
    fl8 = _read(args.flake8)
    if fl8:
        for it in parse_flake8(fl8):
            items.append({
                "ts": _now_iso(),
                "severity": "style",
                "kind": "lint",
                **it,
            })
    if _read_status(args.flake8_status):
        items.append({"ts": _now_iso(), "severity": "info", "tool": "flake8", "message": "flake8 non-zero"})

    # mypy
    mp = _read(args.mypy)
    if mp:
        for it in parse_mypy(mp):
            items.append({
                "ts": _now_iso(),
                "severity": "type",
                "kind": "type-check",
                **it,
            })
    if _read_status(args.mypy_status):
        items.append({"ts": _now_iso(), "severity": "info", "tool": "mypy", "message": "mypy non-zero"})

    # pytest
    pt = _read(args.pytest)
    if pt:
        for it in parse_pytest(pt):
            items.append({
                "ts": _now_iso(),
                "severity": "test",
                "kind": "test",
                **it,
            })
    if _read_status(args.pytest_status):
        items.append({"ts": _now_iso(), "severity": "info", "tool": "pytest", "message": "pytest non-zero"})

    # ensure parent dir
    parent = os.path.dirname(os.path.abspath(args.out))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    with open(args.out, "a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"Wrote {len(items)} items → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
