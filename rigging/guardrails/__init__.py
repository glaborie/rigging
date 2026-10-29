"""The four deterministic built-ins (ADR-0004, ADR-0009).

Registering on import is the decorator pattern by hand: each class carries its
own registry name, so a manifest names `pii_redaction`, not a Python path.
"""

from rigging.guardrails.commitment import CommitmentGuardrail
from rigging.guardrails.consequential import ConsequentialActionGuardrail
from rigging.guardrails.injection import PromptInjectionGuardrail
from rigging.guardrails.pii import PIIRedactionGuardrail
from rigging.registry import DEFAULT_REGISTRY

BUILTIN_GUARDRAILS = (
    PIIRedactionGuardrail,
    PromptInjectionGuardrail,
    ConsequentialActionGuardrail,
    CommitmentGuardrail,
)

for _cls in BUILTIN_GUARDRAILS:
    DEFAULT_REGISTRY.add("guardrail", _cls.name, _cls, replace=True)

__all__ = [
    "BUILTIN_GUARDRAILS",
    "CommitmentGuardrail",
    "ConsequentialActionGuardrail",
    "PIIRedactionGuardrail",
    "PromptInjectionGuardrail",
]

from rigging.guardrails.refund_window import RefundWindowGuardrail  # noqa: E402,F401
