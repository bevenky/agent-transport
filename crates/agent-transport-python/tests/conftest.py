"""Shared pytest configuration for agent_transport adapter tests."""

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-apply asyncio marker to any async test that lacks one."""
    for item in items:
        if (
            "asyncio" not in item.keywords
            and getattr(item.obj, "__code__", None)
            and item.obj.__code__.co_flags & 0x100  # CO_COROUTINE
        ):
            item.add_marker(pytest.mark.asyncio)
