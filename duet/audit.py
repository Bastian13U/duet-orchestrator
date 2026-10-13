"""Metadata-only, hash-chained audit. No prompts, source, or response bodies."""
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone


class Audit:
    FIELDS = {"run_id", "task_id", "sender", "recipient", "phase", "round", "sha256",
              "reservation", "input_tokens", "output_tokens", "passed", "returncode",
              "commit", "branch", "error_type", "mode", "status", "count"}

    def __init__(self, path):
        self.path = Path(path)
        self.previous = "0" * 64
        self.sequence = 0
        if self.path.exists():
            raise ValueError("Audit path must be new")

    def emit(self, event, **fields):
        if set(fields) - self.FIELDS:
            raise ValueError("Unapproved audit field")
        self.sequence += 1
        payload = {"sequence": self.sequence, "at": datetime.now(timezone.utc).isoformat(),
                   "event": event, "previous": self.previous, **fields}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**payload, "hash": digest}, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.previous = digest


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
