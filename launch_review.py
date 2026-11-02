#!/usr/bin/env python3
"""Launch the STATSports local review UI; install dependencies if needed first."""
import importlib.util, subprocess, sys
from pathlib import Path
root=Path(__file__).resolve().parent
missing=[x for x in ('streamlit','plotly','pandas','numpy','reportlab','requests') if importlib.util.find_spec(x) is None]
if missing:
    print('Missing libraries:',', '.join(missing))
    print('Run:',sys.executable,'-m pip install -r',root/'requirements.txt')
    raise SystemExit(1)
subprocess.run([sys.executable,'-m','streamlit','run',str(root/'app.py'),'--server.headless','true'],check=True)
