#!/usr/bin/python3
"""Checkpoint one SQLite database before the ZFS snapshot is taken.

The unit runs this while writers are fenced, so the WAL is flushed to the main
database file and the on-disk tree is consistent. A missing or unreadable
database is an error: the snapshot must not claim a checkpoint it did not take.
"""

from __future__ import annotations

import sqlite3
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: homelab-checkpoint-sqlite <database>", file=sys.stderr)
        return 2
    try:
        connection = sqlite3.connect(f"file:{argv[1]}?mode=rw", uri=True, timeout=30)
    except sqlite3.Error as exc:
        print(f"checkpoint: cannot open {argv[1]}: {exc}", file=sys.stderr)
        return 1
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    except sqlite3.Error as exc:
        print(f"checkpoint: {argv[1]}: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
