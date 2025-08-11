from crew_config import run_crew
from learning.log_history import log_cycle
from core.voting import evaluate_votes
from core.rewrite_module import rewrite_module
from core.extract_code import extract_code_block

def main():
    print("\n🔁 Launching self-reflective AI crew...\n")
    responses = run_crew()
    if not responses:
        print("❌ No se obtuvieron respuestas del Crew. Verifica los errores anteriores.")
        exit()

    print("\n🗳️ Evaluating agent votes...\n")
    approved, reasoning = evaluate_votes(responses)

    # Extraer bloque de código desde la respuesta del creador
    creator_code = extract_code_block(responses["creator"])

    # Descripción resumida de la modificación (puede automatizarse más adelante)
    proposed_change = "Code proposed by creator agent during autonomous review cycle."

    # Ciclo de evaluación completo
    cycle_result = {
        "agents_involved": list(responses.keys()),
        "module_target": "autonomous_agent.py",
        "proposed_change": proposed_change,
        "approved": approved,
        "reasoning": reasoning,
        "feedback": {
            "human_rating": 4 if approved else 2,
            "notes": "Auto-evaluated cycle. Pending human review."
        }
    }

    print(f"\n✅ Change approved: {approved}")
    print("\n📝 Logging cycle result to history...\n")
    log_cycle(cycle_result)

    # Aplicar reescritura solo si fue aprobada y hay código válido
    if creator_code:
        rewrite_module(new_code=creator_code, approved=approved)
    else:
        print("[main] No valid code extracted. Module was not rewritten.")

if __name__ == "__main__":
    main()
