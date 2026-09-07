from datetime import datetime

from fis.domain.models import BatchState
from fis.storage.batch_files import BatchFiles
from fis.storage.repositories import BatchRepository


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


__all__ = ["recover_interrupted"]
