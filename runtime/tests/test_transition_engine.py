from spst_runtime.pipeline.transition_engine import TransitionEngine
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.events.event import Event

def test_trace():
    s = SubjectState()
    out = TransitionEngine().execute(s, None)
    assert out.metadata["last_trace"][0] == "observe"


def test_transition_engine_is_deterministic_and_does_not_mutate_source():
    state = SubjectState(metadata={"version": 7})
    event = Event(type="user_action", payload={"command": "test"})

    first = TransitionEngine().execute(state, event)
    second = TransitionEngine().execute(state, event)

    assert state.metadata == {"version": 7}
    assert first.metadata == second.metadata
    assert first.metadata["last_trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]


def test_transition_engine_records_structured_step_log():
    state = SubjectState()
    event = Event(type="user_action", payload={"command": "test"})

    result = TransitionEngine().execute(state, event)

    assert result.metadata["transition_log"] == [
        {"index": 1, "step": "observe", "event_type": "user_action"},
        {"index": 2, "step": "retrieve", "event_type": "user_action"},
        {"index": 3, "step": "infer", "event_type": "user_action"},
        {"index": 4, "step": "reflect", "event_type": "user_action"},
        {"index": 5, "step": "govern", "event_type": "user_action"},
        {"index": 6, "step": "commit", "event_type": "user_action"},
        {"index": 7, "step": "act", "event_type": "user_action"},
    ]
