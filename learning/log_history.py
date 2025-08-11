import json
from datetime import datetime

HISTORY_FILE = "learning/history.json"

def log_cycle(cycle_data):
    """Agrega un nuevo ciclo al historial de aprendizaje."""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {"cycles": []}

    cycle_data["cycle_id"] = len(history["cycles"]) + 1
    cycle_data["timestamp"] = datetime.utcnow().isoformat() + "Z"
    history["cycles"].append(cycle_data)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"[log_history] Ciclo #{cycle_data['cycle_id']} registrado exitosamente.")

# Ejemplo de uso manual
if __name__ == "__main__":
    new_cycle = {
        "agents_involved": ["creator", "auditor", "ethicist"],
        "module_target": "autonomous_agent.py",
        "proposed_change": "Refactor method structure and add logging.",
        "approved": False,
        "reasoning": {
            "creator": "Suggested logging mechanism.",
            "auditor": "Found performance regressions.",
            "ethicist": "No ethical concern, but logic unclear."
        },
        "feedback": {
            "human_rating": 2,
            "notes": "Change needs refinement and clarification."
        }
    }

    log_cycle(new_cycle)
