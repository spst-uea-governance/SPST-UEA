from abc import ABC, abstractmethod
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.events.event import Event

class Runtime(ABC):
    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @abstractmethod
    def handle(self,event: Event) -> SubjectState: ...
