from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import spst_runtime

print(f"Hello SPST Runtime {spst_runtime.__version__}")
