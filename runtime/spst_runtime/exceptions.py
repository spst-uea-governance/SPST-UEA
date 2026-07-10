class SPSTRuntimeError(Exception):
    """Base runtime exception."""

class StateValidationError(SPSTRuntimeError):
    pass

class GovernanceViolation(SPSTRuntimeError):
    pass
