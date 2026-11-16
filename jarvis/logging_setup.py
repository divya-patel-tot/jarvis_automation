"""Structured JSON-lines logging with secret redaction and size-based rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time

from jarvis import paths
from jarvis.secrets import redact


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = redact(record.getMessage())
            data = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                "lvl": record.levelname,
                "log": record.name,
                "msg": msg[:4000],
            }
            task_id = getattr(record, "task_id", None)
            if task_id:
                data["task_id"] = task_id
            if record.exc_info:
                data["exc"] = redact(self.formatException(record.exc_info))[:4000]
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "lvl": "ERROR",
                               "log": "logging", "msg": f"formatter failed: {exc}"})


class RedactingConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(level: str = "INFO", console: bool = True) -> None:
    paths.ensure_app_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    log_path = paths.logs_dir() / "jarvis.jsonl"
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(RedactingJsonFormatter())
    root.addHandler(fh)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # cp1252 consoles must never crash logging
        except (AttributeError, ValueError):
            pass
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(getattr(logging, level.upper(), logging.INFO))
        ch.setFormatter(RedactingConsoleFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "comtypes", "PIL", "asyncio",
                  "keyring", "keyring.backend", "cursor_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
