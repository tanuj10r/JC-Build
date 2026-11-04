import sys
from pathlib import Path

# Make pipeline.py and config.py importable from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
