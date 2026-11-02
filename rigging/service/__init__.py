"""FastAPI + SSE widget over the event log (ADR-0018).

Needs `rigging[service]`; importing this package without it raises
`ModuleNotFoundError: fastapi`, which `harness serve` turns into an install hint.
"""

from rigging.service.app import create_app

__all__ = ["create_app"]
