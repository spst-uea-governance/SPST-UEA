from spst_runtime.evaluation.metrics import EvaluationResult

def test_esi():
    r=EvaluationResult(1.0,0.5,0.5)
    assert abs(r.esi-0.6666666667)<1e-6
