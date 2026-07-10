from spst_runtime.bus.event_bus import EventBus

class E:
    type="ping"

def test_publish():
    bus=EventBus()
    called=[]
    bus.subscribe("ping",lambda e: called.append(True))
    bus.publish(E())
    assert called==[True]
