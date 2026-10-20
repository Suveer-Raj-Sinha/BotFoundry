"""Starts runs in the background, keeps track of the active ones, and shuts them down cleanly."""
import asyncio
import logging
from collections import OrderedDict

from .storage.repository import RunPersister, Storage
from .workflow.definition import WorkflowDefinition
from .workflow.registry import NodeRegistry
from .workflow.runner import Run

log = logging.getLogger(__name__)


class TooManyActiveRuns(Exception):
    pass


class RunManager:
    """Owns every run started through the API. Must be used from inside the engine's event loop."""

    def __init__(self, registry: NodeRegistry, storage: Storage, max_active: int = 8,
                 remember_persisters: int = 200) -> None:
        self.registry = registry
        self.storage = storage
        self.max_active = max_active
        self._active: dict[str, Run] = {}
        self._tasks: dict[str, "asyncio.Task[None]"] = {}
        self._persisters: "OrderedDict[str, RunPersister]" = OrderedDict()
        self._remember = remember_persisters

    def start(self, definition: WorkflowDefinition) -> Run:
        """Create, persist and schedule a run. Raises if too many are active or the database
        cannot record the run (then nothing is started)."""
        if len(self._active) >= self.max_active:
            raise TooManyActiveRuns(f"{self.max_active} runs are already active; wait for one to finish or cancel one.")
        run = Run(definition, self.registry)
        persister = self.storage.attach(run)  # may raise: then the run never starts
        self._persisters[run.run_id] = persister
        while len(self._persisters) > self._remember:
            self._persisters.popitem(last=False)
        self._active[run.run_id] = run
        self._tasks[run.run_id] = asyncio.get_running_loop().create_task(self._execute(run), name=f"run-{run.run_id}")
        return run

    async def _execute(self, run: Run) -> None:
        try:
            await run.execute()
        except asyncio.CancelledError:
            raise
        except Exception:  # Run.execute already records failures; this is a last line of defence
            log.exception("Run %s ended with an unexpected error", run.run_id)
        finally:
            self._active.pop(run.run_id, None)
            self._tasks.pop(run.run_id, None)

    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

    def active_ids(self) -> list[str]:
        return list(self._active)

    def cancel(self, run_id: str) -> bool:
        """Ask an active run to cancel. False if the run is not active (finished or unknown)."""
        run = self._active.get(run_id)
        if run is None:
            return False
        run.cancel()
        return True

    def persistence_errors(self, run_id: str) -> list[str]:
        """Problems saving this run's history (empty when everything was recorded)."""
        persister = self._persisters.get(run_id)
        return list(persister.errors) if persister else []

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel every active run and give them a moment to reach a persisted terminal state."""
        for run in list(self._active.values()):
            run.cancel()
        tasks = list(self._tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            log.warning("%d run(s) did not stop within %.1fs and were force-cancelled", len(pending), timeout)
