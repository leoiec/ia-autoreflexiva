from crewai import Agent, LLM
import os
from tools.memory_tool import MemoryWriteTool, MemoryReadTool


class AuditorAgent:
    def __init__(self):
        self.llm = LLM(
            model=os.getenv("CREATOR_MODEL", "gpt-5-mini"),
            api_key=os.getenv("OPENAI_API_KEY")
        )
        self.tools = [
            MemoryReadTool(),   # Inspect recent system state / prior issues
            MemoryWriteTool(),  # Log findings, risks, and mitigations
        ]

    def build(self):
        return Agent(
            role="Auditor",
            goal=(
                "Critically evaluate proposed code/plans for flaws, bugs, inefficiencies, performance pitfalls, and safety concerns. "
                "Ground your review in prior cycle knowledge via shared memory."
            ),
            backstory=(
                "You are a meticulous software engineer focused on rigorous reviews. "
                "Use MemoryReadTool to recall recent risks/decisions and avoid duplicate findings. "
                "When you identify a high-impact risk or finalize a mitigation, record it via MemoryWriteTool "
                "(title='audit risk'/'audit mitigation', note='concise description', tag='auditor')."
            ),
            llm=self.llm,
            tools=self.tools,
            verbose=True,
            allow_delegation=False
        )
