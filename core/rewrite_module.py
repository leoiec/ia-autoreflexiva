import os
from datetime import datetime
from shutil import copyfile

MODULE_PATH = "modules/autonomous_agent.py"
BACKUP_DIR = "memory/versioning/"

def backup_module():
    """Crea una copia del archivo actual con timestamp."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"autonomous_agent_{timestamp}.py"
    os.makedirs(BACKUP_DIR, exist_ok=True)
    copyfile(MODULE_PATH, os.path.join(BACKUP_DIR, backup_name))
    print(f"[rewrite_module] Backup created at: {backup_name}")
    return backup_name

def rewrite_module(new_code: str, approved: bool):
    """Reescribe el módulo solo si fue aprobado por votación."""
    if not approved:
        print("[rewrite_module] Change NOT approved. No rewrite will occur.")
        return False

    backup_module()

    with open(MODULE_PATH, "w", encoding="utf-8") as f:
        f.write(new_code)

    print("[rewrite_module] Module successfully updated.")
    return True
