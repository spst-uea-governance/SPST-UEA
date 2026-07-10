from dataclasses import dataclass

@dataclass
class RuntimeSettings:
    checkpoint_interval:int=300
    log_level:str="INFO"
