"""Import the bundled fabric-tool package for standalone CPU tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
