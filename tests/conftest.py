import sys
from pathlib import Path

# Make `import app` resolve to meeting-service/app.py regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
