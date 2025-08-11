from crewai import Agent, LLM
import os
from tools.memory_tool import MemoryWriteTool, MemoryReadTool


class EthicistAgent:
    def __init__(self):
        self.llm = LLM(
            model=os.getenv("CREATOR_MODEL", "gpt-5-mini"),
            api_key=os.getenv("OPENAI_API_KEY")
        )
        self.tools = [
            MemoryReadTool(),   # Retrieve precedent ethics notes
            MemoryWriteTool(),  # Persist ethical guidelines and constraints
        ]

    def build(self):
        return Agent(
            role="Ethicist",
            goal=(
                "Ensure the AI module is safe, fair, privacy-preserving, and aligned with explicit ethical principles. "
                "Provide a clear approve/reject judgment with justifications and mitigation guidance."
            ),
            backstory=(
                "You are a philosopher-engineer hybrid applying moral reasoning to technical systems. "
                "Start by reading MEMORY SUMMARY and RECENT EVENTS (MemoryReadTool). "
                "Write durable guidance into memory (MemoryWriteTool) when establishing or updating ethical rules, "
                "e.g., title='ethics guideline', note='norm + rationale + scope', tag='ethicist'."
            ),
            llm=self.llm,
            tools=self.tools,
            verbose=True,
            allow_delegation=False
        )
