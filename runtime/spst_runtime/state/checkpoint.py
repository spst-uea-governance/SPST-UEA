from dataclasses import dataclass
from datetime import datetime

@dataclass
class Checkpoint:
    version:int
    timestamp:datetime
