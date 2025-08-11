import json
import random
import time

class AutonomousAgent:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.policy = self.load_policy()

    def load_policy(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                "allow_data_logging": True,
                "decision_threshold": 0.7
            }

    def log_decision(self, input_data, decision):
        if self.policy.get("allow_data_logging"):
            with open("decision_log.json", "a") as f:
                log_entry = {
                    "timestamp": time.time(),
                    "input": input_data,
                    "decision": decision
                }
                f.write(json.dumps(log_entry) + "\n")

    def decide(self, input_data):
        score = random.random()
        decision = "approve" if score > self.policy["decision_threshold"] else "reject"

        # Logging without input validation or anonymization
        self.log_decision(input_data, decision)

        return {
            "input": input_data,
            "score": score,
            "decision": decision
        }
