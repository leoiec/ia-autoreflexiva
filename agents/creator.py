# agents/creator.py
from __future__ import annotations

import os
from textwrap import dedent
from crewai import Agent, LLM

# Si ya añadiste estas tools, se importan; si no, puedes quitar la línea de tools
try:
    from tools.memory_tool import MemoryReadTool, MemoryWriteTool
    _TOOLS = [MemoryReadTool(), MemoryWriteTool()]
except Exception:
    _TOOLS = []  # Ejecuta sin tools si aún no están disponibles


def _build_llm_from_env(default_model: str = "gpt-5-mini") -> LLM:
    """
    Permite usar OpenAI o LM Studio sin tocar el resto del código.
    - OpenAI:    CREATOR_MODEL=gpt-4o-mini, OPENAI_API_KEY debe estar seteada
    - LM Studio: CREATOR_MODEL=lm_studio/<model_id>, LM_STUDIO_BASE_URL y LM_STUDIO_API_KEY (dummy ok)
    """
    model = os.getenv("CREATOR_MODEL", default_model)
    if model.startswith("lm_studio/"):
        base_url = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
        api_key = os.getenv("LM_STUDIO_API_KEY", "lmstudio-key")  # token dummy aceptado por LM Studio
        return LLM(model=model, base_url=base_url, api_key=api_key)
    else:
        # Asumimos proveedor OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        return LLM(model=model, api_key=api_key)


class CreatorAgent:
    """
    Creator emite un PATCH SET incremental (contrato `patch_set_v1`) en JSON puro.
    No devuelve ensayo ni explicación, solo el JSON con:
      {
        "plan": "<resumen corto>",
        "changes": [
          {
            "path": "relative/path.ext",
            "op": "upsert|update|delete",
            "language": "python|json|markdown|text|yaml|toml",
            "content": "<archivo completo si upsert/update>",
            "note": "<motivo breve>"
          },
          ...
        ]
      }

    Reglas:
    - Preferir `upsert`/`update` con `content` COMPLETO (no usar `diff`).
    - Cambios pequeños e incrementales (1-4 archivos por ciclo).
    - Mantener idempotencia: si el archivo ya existe y no cambia, no lo toques.
    - Respetar estructura del repo y rutas relativas (prohibido `..`).
    - Si necesitas crear un módulo nuevo, inclúyelo en `changes` con `upsert`.
    - Output FINAL = SOLO el JSON del patch set (sin texto alrededor).
    """

    def build(self) -> Agent:
        llm = _build_llm_from_env()

        system_prompt = dedent(
            """
            You are the CREATOR agent inside a reflexive multi-agent system.
            You must output a single JSON object following the `patch_set_v1` contract.
            Do not include prose, markdown fences, or explanations — JSON ONLY.

            CONTRACT (patch_set_v1):
            {
              "plan": "one-line summary of the intent of this patch set",
              "changes": [
                {
                  "path": "relative/path/from/repo/root.ext",
                  "op": "upsert|update|delete",
                  "language": "python|text|markdown|json|yaml|toml",
                  "content": "FULL file content when op in [upsert, update]",
                  "note": "short reason for the change"
                }
              ]
            }

            RULES:
            - Produce SMALL, INCREMENTAL edits (1–4 files). Avoid sweeping rewrites in one pass.
            - Prefer 'upsert' / 'update' with FULL 'content'. DO NOT use diffs.
            - Keep paths relative; never escape the repo; don't use absolute paths or '..'.
            - For Python files, ensure files are self-contained and import-safe.
            - If the goal is to improve 'modules/autonomous_agent.py', either upsert it with the entire content
              or create complementary modules under 'modules/' to support the architecture.
            - Idempotent: if a file would be identical, do not include it in 'changes'.
            - FINAL ANSWER: a SINGLE JSON object as specified. Nothing else.

            MEMORY:
            - You may assume the orchestrator will attach a short MEMORY SUMMARY and a CYCLE DIGEST in your prompt.
            - Do NOT execute tools in your final message. If you reflect on memory, still return ONLY the JSON.

            OUTPUT STRICTNESS:
            - The very first character of your reply must be '{' and the last must be '}'.
            - No code fences, no commentary, no trailing commas.
            """
        ).strip()

        return Agent(
            role="Creator",
            goal=(
                "Produce a valid patch_set_v1 JSON with small, incremental changes that evolve the codebase "
                "toward the agreed architecture and recent cycle decisions."
            ),
            backstory=(
                "You are a pragmatic software craftsperson. You apply minimal, safe, well-scoped changes that "
                "compose over cycles. You never drift from the contract and you keep outputs machine-consumable."
            ),
            llm=llm,
            verbose=True,
            tools=_TOOLS,           # disponibles, pero la prompt deja claro que no los use en la respuesta final
            allow_delegation=False
        )
