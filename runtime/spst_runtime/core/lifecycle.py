from enum import Enum

class RuntimePhase(str,Enum):
    UNINITIALIZED="uninitialized"
    INITIALIZING="initializing"
    ACTIVE="active"
    PAUSED="paused"
    DEGRADED="degraded"
    RECOVERING="recovering"
    ARCHIVED="archived"
