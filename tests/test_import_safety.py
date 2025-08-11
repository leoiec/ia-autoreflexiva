import importlib
import os
import sys
from pathlib import Path

def test_import_has_no_side_effects(tmp_path, monkeypatch):
    # Set a temp HOME to avoid writing user dirs
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    # Block any consent-by-env so imports stay inert
    monkeypatch.delenv("AGENT_LOAD_CONSENT", raising=False)

    # Ensure no ledger file exists before import
    before = set(Path(".").rglob("*"))

    mod = importlib.import_module("modules.autonomous_agent")
    assert hasattr(mod, "__version__")

    # Import should not create new files or dirs
    after = set(Path(".").rglob("*"))
    assert before == after, "Import created files/directories (side-effect)."

def test_legacy_file_absent():
    assert not Path("modules/autonomous_agent.py").exists(), \
        "Legacy single-file module must be removed."
