"""Top-level package for the ScholarLoop backend.

``CONTRACT_VERSION`` is the single number every other step must agree on. Bump it
only via a CONTRACT-CHANGE-REQUEST; the schema export and the drift test read it.
"""

from __future__ import annotations

__all__ = ["CONTRACT_VERSION", "__version__"]

__version__ = "0.1.0"
CONTRACT_VERSION = 1
