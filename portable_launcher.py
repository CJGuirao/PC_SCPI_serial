"""Launch the scope with local runtime state and isolated Python imports."""
import os
from pathlib import Path
import runpy
import sys

PROJECT = Path(__file__).resolve().parent
RUNTIME = PROJECT / ".portable"
for name in ("cache", "tmp", "data", "logs", "appdata", "localappdata"):
    (RUNTIME / name).mkdir(parents=True, exist_ok=True)

os.environ.update({
    "MPLCONFIGDIR": str(RUNTIME / "cache"),
    "TEMP": str(RUNTIME / "tmp"),
    "TMP": str(RUNTIME / "tmp"),
    "APPDATA": str(RUNTIME / "appdata"),
    "LOCALAPPDATA": str(RUNTIME / "localappdata"),
})
os.chdir(RUNTIME / "data")
# -I excludes inherited Python paths and the user site. Only add this project.
sys.path.insert(0, str(PROJECT))
sys.dont_write_bytecode = True
log = open(RUNTIME / "logs" / "application.log", "a", encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log
try:
    runpy.run_path(str(PROJECT / "main.py"), run_name="__main__")
except Exception:
    import traceback
    traceback.print_exc()
    raise

