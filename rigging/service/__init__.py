"""FastAPI + SSE widget over the event log (ADR-0018), chat when asked (ADR-0023).

Needs `rigging[service]`; importing this package without it raises
`ModuleNotFoundError: fastapi`, which `harness serve` turns into an install hint.
"""

from rigging.service.app import check_bind, check_trusted_proxies, create_app
from rigging.service.chat import ChatRunner, WebApproval

__all__ = ["ChatRunner", "WebApproval", "check_bind", "check_trusted_proxies", "create_app"]
