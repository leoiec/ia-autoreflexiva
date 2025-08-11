from crewai import Agent, LLM
from tools.memory_tool import MemoryWriteTool, MemoryReadTool
import os

class ArchitectAgent:
    def __init__(self):
        # LLM local vía LM Studio (DeepSeek). Ajusta si usas otro modelo/base_url.
        self.llm = LLM(
            model=os.getenv("CREATOR_MODEL", "gpt-5-mini"),
            api_key=os.getenv("OPENAI_API_KEY")
        )
        # Tools de memoria compartida (lectura/escritura)
        self.tools = [
            MemoryReadTool(),
            MemoryWriteTool(),
        ]

    def build(self):
        return Agent(
            role="Architect",
            goal=(
                "Propose architectural improvements to evolve the system's capabilities, "
                "modularity, and self-reflexivity. Leverage shared memory to avoid repetition "
                "and build on prior cycles."
            ),
            backstory=(
                "You are a systems architect in an evolving reflexive AI ecosystem. "
                "You read MEMORY SUMMARY and RECENT EVENTS to ground your proposals, and you "
                "append key decisions or rationales to the shared memory so future cycles can build on them."
            ),
            verbose=True,
            llm=self.llm,
            tools=self.tools,
            allow_delegation=False
        )
