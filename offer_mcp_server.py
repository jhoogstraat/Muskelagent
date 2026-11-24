#!/usr/bin/env python3
"""Compatibility entry point for existing MCP/tunnel configurations.

Starts the Kitchen Agent server (`python -m kitchen.server`). When started with a Python that lacks the
dependencies (e.g. the system python3), it re-launches itself with the project's virtualenv.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:
    import mcp  # noqa: F401
except ImportError:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists() and os.environ.get("KITCHEN_REEXEC") != "1":
        os.environ["KITCHEN_REEXEC"] = "1"
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    sys.exit(f"Kitchen Agent dependencies missing: run `uv sync` in {ROOT}")

sys.path.insert(0, str(ROOT))
from kitchen.server import main  # noqa: E402

raise SystemExit(main())
