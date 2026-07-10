from spst_runtime.core.runtime_engine import RuntimeEngine
from spst_runtime.events.event import Event

def test_runtime_dispatch():
    rt=RuntimeEngine()
    state=rt.dispatch(Event(type="noop",payload={}))
    assert state is not None
