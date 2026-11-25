# Lets the scripts import bp_model when run as `python scripts/xxx.py`.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
