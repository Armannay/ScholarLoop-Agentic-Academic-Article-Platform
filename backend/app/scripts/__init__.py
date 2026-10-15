"""Operator scripts: schema export and the CI secret scan.

Deliberately import-free. Importing the submodules here would put them in
``sys.modules`` before ``python -m app.scripts.export_schemas`` executes, which makes
runpy warn (and can execute a module twice).
"""

from __future__ import annotations

__all__: list[str] = []
