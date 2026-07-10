from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spst_runtime.runtime.runtime_loop import RuntimeLoop

rt = RuntimeLoop()
rt.start()
for _ in range(3):
    rt.step()
rt.stop()
print(rt.ctx)
