# tests/test_agent.py

import unittest
import sys
import os

# Asegurarse de que el módulo esté accesible al importar
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'modules')))

from autonomous_agent import AutonomousAgent

class TestAutonomousAgent(unittest.TestCase):
    
    def setUp(self):
        self.agent = AutonomousAgent()
    
    def test_decide_improve(self):
        result = self.agent.decide("improve")
        self.assertEqual(result, "Applying improvement")

    def test_decide_analyze(self):
        result = self.agent.decide("analyze")
        self.assertEqual(result, "Analyzing system")
    
    def test_decide_default(self):
        result = self.agent.decide("anything else")
        self.assertEqual(result, "Default behavior")

if __name__ == "__main__":
    unittest.main()
