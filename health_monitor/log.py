"""Rich log panel: a logging handler that keeps recent records for the TUI."""

from __future__ import annotations

import logging
from collections import deque

from rich.text import Text

LEVEL_STYLES = {
    logging.DEBUG: "grey58",
    logging.INFO: "grey74",
    logging.WARNING: "yellow3",
    logging.ERROR: "bold red3",
    logging.CRITICAL: "bold white on red3",
}


class PanelHandler(logging.Handler):
    """Ring buffer of recent log records, rendered as rich Text lines."""

    def __init__(self, maxlen: int = 150):
        super().__init__()
        self.lines: deque[Text] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        txt = Text(msg)
        txt.stylize(LEVEL_STYLES.get(record.levelno, ""))
        self.lines.append(txt)

    def render(self, n: int = 6) -> list[Text]:
        return list(self.lines)[-n:]


def setup_logging(verbose: bool = False, log_file: str | None = None) -> PanelHandler:
    root = logging.getLogger("hl-traf")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    panel = PanelHandler()
    panel.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(panel)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)

    return panel
