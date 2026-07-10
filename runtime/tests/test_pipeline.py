from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.events.event import Event

def test_pipeline():
    p = StateTransitionPipeline()
    assert p.run({}, None) == {}


def test_pipeline_commits_transition_version_after_governance():
    pipeline = StateTransitionPipeline()
    result = pipeline.run(
        SubjectState(),
        Event(type="user_action", payload={"command": "test"}),
    )

    assert result.metadata["version"] == 1
    assert result.metadata["last_trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
