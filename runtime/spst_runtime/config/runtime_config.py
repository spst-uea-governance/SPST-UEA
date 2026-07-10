from dataclasses import dataclass

@dataclass
class RuntimeConfig:
    runtime_name:str="spst-runtime"
    autosave:bool=True
    evaluation_enabled:bool=True
