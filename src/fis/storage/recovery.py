import logging
from datetime import datetime

from fis.domain.models import BatchState
from fis.storage.batch_files import BatchFiles
from fis.storage.repositories import BatchRepository

_LOGGER = logging.getLogger(__name__)


def recover_interrupted(repository: BatchRepository, files: BatchFiles, now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")

    for batch in repository.list_interrupted():
        if batch.state not in {BatchState.PARSING, BatchState.GENERATING}:
            continue
        files.clear_work(batch.id)
        repository.set_state(
            batch.id,
            BatchState.INCOMPLETE,
            now,
            error_code="INTERRUPTED",
            error_detail="上次运行意外中断，可重新生成。",
        )


def discard_batch_working_files(files: BatchFiles) -> None:
    """Remove every batch working directory.

    A batch directory only holds the uploaded source, and only the in-memory
    workspace ever reads it. ``WorkspaceStore`` is per-process and nothing
    rebuilds a workspace from the database, so at startup none of these
    directories is reachable — left alone they accumulate forever, which is how
    twelve batches grew to 81 MB here.

    Failures are logged and skipped: this is housekeeping, and a directory the
    platform will not release must not stop FIS from starting.
    """

    for batch_id in files.list_batch_ids():
        try:
            files.delete_batch(batch_id)
        except (OSError, ValueError):
            _LOGGER.warning("could not remove batch working directory %s", batch_id)


def run_startup_maintenance(
    repository: BatchRepository, files: BatchFiles, now: datetime
) -> None:
    """Settle the previous run before serving: mark it, then clear its files."""

    recover_interrupted(repository, files, now)
    discard_batch_working_files(files)


__all__ = [
    "discard_batch_working_files",
    "recover_interrupted",
    "run_startup_maintenance",
]
