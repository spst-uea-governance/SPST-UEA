class Rule:
    def __init__(self, name, check):
        self.name = name
        self.check = check

class RuleEngine:
    def __init__(self):
        self.rules = []

    def register(self, rule):
        self.rules.append(rule)

    def evaluate(self, context):
        return all(rule.check(context) for rule in self.rules)
