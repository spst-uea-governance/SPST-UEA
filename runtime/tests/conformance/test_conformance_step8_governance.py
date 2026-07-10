"""
Conformance Test for Codex Step 8: Governance Decision Interface.
Authority Order: 4. tests (Must satisfy RFC-0004 & normative specification).
Codex MUST NOT bypass governance for convenience.
"""
import pytest
from spst_runtime.engines.governance_engine import GovernanceEngine

@pytest.mark.conformance
def test_conformance_step8_governance_rejection():
    """
    Verify that GovernanceEngine intercepts and rejects unauthorized state transitions.
    If a Rule check fails, authorize() must return False or raise SPSTRuntimeError.
    """
    engine = GovernanceEngine()
    
    unauthorized_action = {
        "type": "commit_identity_change",
        "source": "model_output_direct",
        "provenance_verified": False
    }
    
    authorized = engine.authorize(unauthorized_action)
    assert authorized is False, "GovernanceEngine MUST reject direct unverified model identity changes."
