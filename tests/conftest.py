"""Test session setup.

Force the hermetic SQLite backend and disable Langfuse for tests, regardless of a local
`.env` (pydantic-settings ranks real env vars above the .env file, and these are set
before `src.config.get_settings` is first imported/called). This keeps unit tests fast,
offline, and free of Postgres / trace side effects.
"""

from __future__ import annotations

import os

os.environ["DATA_BACKEND"] = "sqlite"
os.environ["LANGFUSE_ENABLED"] = "false"
