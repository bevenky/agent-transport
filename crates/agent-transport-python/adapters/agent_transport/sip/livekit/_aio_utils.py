"""Async utilities matching LiveKit's utils.aio."""

import asyncio
import functools
import inspect
from typing import Any, Callable


def _release_waiter(waiter: asyncio.Future[Any], *_: Any) -> None:
    if not waiter.done():
        waiter.set_result(None)


async def cancel_and_wait(*futures: asyncio.Future[Any]) -> None:
    """Cancel futures and wait for them to complete.

    Exact copy of LiveKit's utils.aio.cancel_and_wait.
    """
    loop = asyncio.get_running_loop()
    waiters = []

    for fut in futures:
        waiter = loop.create_future()
        cb = functools.partial(_release_waiter, waiter)
        waiters.append((waiter, cb))
        fut.add_done_callback(cb)
        fut.cancel()

    try:
        for waiter, _ in waiters:
            await waiter
    finally:
        for i, fut in enumerate(futures):
            _, cb = waiters[i]
            fut.remove_done_callback(cb)


async def call_setup(setup_fnc: Callable, proc: Any) -> None:
    """Invoke a user-provided setup function, supporting both sync and async.

    Supports both calling conventions:
    - New: ``setup_fnc(proc)`` — function receives a JobProcess-like object
           whose ``userdata`` dict should be populated in place.
    - Old: ``setup_fnc()`` that returns a dict — the returned dict is
           assigned to ``proc.userdata``.

    Gracefully handles coroutine functions (``async def``) and regular
    functions that return awaitables (e.g., ``return asyncio.gather(...)``).

    Raises any exception from the setup function so the caller can log
    and abort startup cleanly.
    """
    is_coro = inspect.iscoroutinefunction(setup_fnc)

    # Try new pattern: setup_fnc(proc)
    try:
        result = setup_fnc(proc) if not is_coro else await setup_fnc(proc)
    except TypeError:
        # Old pattern: setup_fnc() -> dict
        result = setup_fnc() if not is_coro else await setup_fnc()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            proc.userdata = result
        return

    # New-pattern result: if it's an awaitable (e.g., sync fn returning coroutine),
    # await it. Otherwise it's None (fn mutated proc.userdata directly).
    if inspect.isawaitable(result):
        await result
