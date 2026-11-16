"""Test helpers for opening a session or gateway database directly."""

import sqlite3
from contextlib import contextmanager


@contextmanager
def connected(path, **kwargs):
    """A connection that commits like `with sqlite3.connect(...)` and is always closed:
    the connection's own context manager commits but never closes."""
    database = sqlite3.connect(path, **kwargs)
    try:
        with database:
            yield database
    finally:
        database.close()
