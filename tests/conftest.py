from __future__ import annotations

import pytest

from rigging.registry import Registry


@pytest.fixture()
def registry() -> Registry:
    return Registry()
