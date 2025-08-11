# core/voting.py

from typing import Dict, Tuple, List

def evaluate_votes(responses: Dict[str, str], show_summary: bool = True) -> Tuple[bool, Dict[str, str]]:
    """
    Evalúa si las respuestas de los agentes indican aprobación.
    También extrae y retorna las justificaciones textuales.

    :param responses: dict con claves como ["architect","revolutionary","creator","auditor","ethicist"]
                      y valores con sus respuestas textuales.
    :param show_summary: si True, imprime un resumen con el voto de cada agente.
    :return: (approved: bool, reasoning: dict[agent -> respuesta])
    """
    reasoning: Dict[str, str] = {}
    votes: Dict[str, bool] = {}

    # Palabras/expresiones que suelen implicar aprobación o rechazo.
    positive_keywords: List[str] = [
        "approve", "approved", "approves", "approval",
        "agree", "agrees", "agreement",
        "valid", "acceptable", "safe", "no issues",
        "looks good", "lgtm", "ship it",
        # Español
        "apruebo", "aprueba", "aprobado", "de acuerdo",
        "válido", "valido", "aceptable", "seguro", "sin problemas"
    ]
    negative_keywords: List[str] = [
        "reject", "rejected", "rejects",
        "disagree", "disagrees",
        "problem", "issue", "risk", "unsafe",
        "not approve", "do not approve", "does not approve",
        # Español
        "rechazo", "rechaza", "rechazado",
        "en desacuerdo", "problema", "riesgo", "inseguro",
        "no apruebo", "no aprueba"
    ]

    def decide_approval(text: str) -> bool:
        t = (text or "").lower()
        pos = any(k in t for k in positive_keywords)
        neg = any(k in t for k in negative_keywords)
        if pos and not neg:
            return True
        if neg and not pos:
            return False
        # Empate/ambigüedad: por defecto rechazamos para ser conservadores
        return False

    # Calcular votos y reasoning
    for agent, response in responses.items():
        reasoning[agent] = (response or "").strip()
        votes[agent] = decide_approval(response or "")

    all_approved = all(votes.values()) if votes else False

    if show_summary:
        # Orden preferente para mostrar
        preferred_order = ["architect", "revolutionary", "creator", "auditor", "ethicist"]
        ordered_agents = [a for a in preferred_order if a in votes] + [a for a in votes.keys() if a not in preferred_order]

        print("\n=== Votación de agentes sobre la propuesta ===")
        for agent in ordered_agents:
            mark = "✅ Aprobó" if votes.get(agent, False) else "❌ Rechazó"
            print(f"{agent.capitalize():<13} {mark}")
        print(f"\n🗳️ Cambio aprobado: {all_approved}")

    return all_approved, reasoning
