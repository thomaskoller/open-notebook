"""Celery task definitions for Open Notebook.

The worker starts via
``celery -A open_notebook.celery_app:celery worker`` with ``include``
pointing here, so importing this package is what registers the tasks.

``open_notebook.celery_app`` calls ``ensure_internal_no_proxy()`` at import
time - before anything connects to SurrealDB or Redis - so the internal
websocket/broker connections are never tunnelled through a configured HTTP
proxy (issue #1160).
"""

from .embedding_commands import (
    create_insight_command,
    embed_insight_command,
    embed_note_command,
    embed_source_command,
    rebuild_embeddings_command,
)
from .podcast_commands import generate_podcast_command
from .source_commands import process_source_command, run_transformation_command

__all__ = [
    # Embedding commands
    "embed_note_command",
    "embed_insight_command",
    "embed_source_command",
    "rebuild_embeddings_command",
    "create_insight_command",
    # Other commands
    "generate_podcast_command",
    "process_source_command",
    "run_transformation_command",
]
