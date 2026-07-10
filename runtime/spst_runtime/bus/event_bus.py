from collections import defaultdict
from typing import Callable

class EventBus:
    def __init__(self):
        self._handlers=defaultdict(list)
        self.published=[]

    def subscribe(self,event_type:str,handler:Callable):
        self._handlers[event_type].append(handler)

    def publish(self,event):
        self.published.append(event)
        for h in self._handlers.get(event.type,[]):
            h(event)
