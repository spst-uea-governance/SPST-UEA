from abc import ABC, abstractmethod

class PersistenceRepository(ABC):
    @abstractmethod
    async def save(self,key:str,value:dict)->None: ...
    @abstractmethod
    async def load(self,key:str)->dict|None: ...
