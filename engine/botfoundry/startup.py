"""Engine start-up steps that must happen before serving requests."""
import logging
from pathlib import Path

from .config import DATABASE_FILE
from .storage import Storage

log = logging.getLogger(__name__)


def open_storage(data_dir: Path) -> tuple[Storage, list[str]]:
    """Open the database (migrating it if needed) and fail any run that a crash left unfinished.
    Returns the storage and the ids of the runs that were marked interrupted."""
    storage = Storage.open(Path(data_dir) / DATABASE_FILE)
    interrupted = storage.mark_interrupted_runs()
    if interrupted:
        log.warning("Marked %d interrupted run(s) as failed: %s", len(interrupted), ", ".join(interrupted))
    return storage, interrupted
