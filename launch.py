"""Simple launcher — run this file to start the app."""
import subprocess, sys, os
from pathlib import Path

here = Path(__file__).parent
app  = here / "app.py"

os.chdir(here)
subprocess.run([sys.executable, str(app)])
