"""Run blocking filesystem work in a worker thread without freezing the engine, and make sure that thread
stops when the node is cancelled, times out, or the run ends."""
import asyncio
from typing import Any, Callable, TypeVar

from ..workflow.node import NodeContext, RunCancelled
from .fsutil import StopFlag, Stopped

T = TypeVar("T")


async def run_blocking(ctx: NodeContext, work: Callable[[StopFlag], T]) -> T:
    stop = StopFlag(lambda: ctx.cancel.cancelled)
    try:
        return await asyncio.to_thread(work, stop)
    except asyncio.CancelledError:
        stop.set()  # the thread notices at its next check; it never keeps working in the background
        raise
    except Stopped:
        raise RunCancelled() from None
