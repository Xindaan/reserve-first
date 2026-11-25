"""Aufruf: .venv/bin/python scripts/analyze.py private/cloud/telemetry.sqlite"""

import json
import sys
from pathlib import Path

from solarbank_controller.analysis import analyze

print(json.dumps(analyze(Path(sys.argv[1])), indent=2))
