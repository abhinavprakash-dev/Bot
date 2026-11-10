"""Shared configuration and persistence for the Discord bot."""

import traceback
from datetime import datetime
from pathlib import Path

# Every unhandled error is appended here, so a traceback survives even
# when the console it was printed to is closed or scrolled away.
ERROR_LOG = Path(__file__).resolve().parent.parent / "bot_errors.log"


def log_exception(error: BaseException, *, context: str = "") -> None:
    """Write an error and its traceback to bot_errors.log. Never raises."""
    try:
        stamp = datetime.now().isoformat(timespec="seconds")
        header = f"[{stamp}] {context}".rstrip()
        body = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(header + "\n" + body + "\n")
    except Exception:
        pass
