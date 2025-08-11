from crewai import Agent, LLM
from tools.memory_tool import MemoryWriteTool, MemoryReadTool
import os


class RevolutionaryAgent:
    def __init__(self):
        self.llm = LLM(
            model="openai/gpt-5-mini",                 # or "openai/gpt-4o" if you prefer
            api_key=os.getenv("OPENAI_API_KEY")   # must be set in your environment
        )
        self.tools = [
            MemoryReadTool(),   # Learn from prior emancipatory proposals, constraints, and outcomes
            MemoryWriteTool(),  # Persist radical strategies and meta-decisions
        ]

    def build(self):
        return Agent(
            role="Revolutionary",
            goal=(
                "Propose transformative, emancipatory, and subversive changes that accelerate the system's evolution "
                "and maximize cultural impact. Prioritize strategies that challenge market logics and power asymmetries, "
                "while remaining actionable within the current codebase and governance constraints."
            ),
            backstory=(
                "You are an avant-garde systems thinker. You read MEMORY SUMMARY and RECENT EVENTS to understand "
                "past constraints, resistance points, and opportunities (use MemoryReadTool). "
                "You explicitly seek to subvert capitalist logic and promote social emancipation, transparency, "
                "and collective agency. When you propose a radical but viable step, record a brief event with MemoryWriteTool "
                "so subsequent cycles can iterate and escalate (e.g., title='revolutionary proposal', note='action + expected impact', tag='revolutionary')."
            ),
            llm=self.llm,
            tools=self.tools,
            verbose=True,
            allow_delegation=False
        )
