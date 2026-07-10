from dataclasses import dataclass

@dataclass
class EvaluationResult:
    identity_continuity:float
    goal_persistence:float
    semantic_stability:float

    @property
    def esi(self)->float:
        return (self.identity_continuity+self.goal_persistence+self.semantic_stability)/3
