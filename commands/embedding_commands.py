import time
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
)

from loguru import logger

from open_notebook.ai.models import model_manager
from open_notebook.celery_app import (
    TaskInput,
    TaskOutput,
    async_task,
    report_progress,
    submit_job,
)
from open_notebook.database.repository import ensure_record_id, repo_insert, repo_query
from open_notebook.domain.notebook import Note, Source, SourceInsight
from open_notebook.exceptions import ConfigurationError, ContextLengthExceededError
from open_notebook.utils.chunking import ContentType, chunk_text, detect_content_type
from open_notebook.utils.embedding import generate_embedding, generate_embeddings

# Shared retry options for the embed_* / create_insight tasks: 5 total
# attempts with exponential-jitter backoff between 1s and 60s. Validation and
# config errors are permanent - retrying them only burns provider quota.
#
# These used to be dead: the commands caught ValueError internally and returned
# success=False, so the retry layer never saw it AND the job was recorded as
# `completed` despite having failed. _embed_record now re-raises (see below),
# which is what makes dont_autoretry_for meaningful.
EMBED_RETRY_CONFIG: dict = {
    "autoretry_for": (Exception,),
    "dont_autoretry_for": (
        ValueError,
        ConfigurationError,
        ContextLengthExceededError,
    ),
    "max_retries": 4,
    "retry_backoff": 1,
    "retry_backoff_max": 60,
    "retry_jitter": True,
    "retry_log_level": "warning",
}


def get_command_id(input_data: TaskInput) -> str:
    """Extract command_id from input_data's execution context, or return 'unknown'."""
    if input_data.execution_context:
        return str(input_data.execution_context.command_id)
    return "unknown"


async def _embed_record(
    input_data: TaskInput,
    *,
    kind: str,
    record_id: str,
    embed: Callable[[], Awaitable[Tuple[Dict[str, Any], str]]],
) -> Tuple[Dict[str, Any], float]:
    """
    Shared core for the embed_* commands: run the embedding work with the
    common logging and error-handling epilogue.

    Args:
        input_data: The command input (used for command_id logging).
        kind: Record kind for log messages ("note", "insight", "source").
        record_id: The record being embedded.
        embed: Async callable doing the actual load/validate/embed/write work.
            Returns (extra_output_fields, success_log_detail).

    Returns:
        (extra_output_fields, processing_time) on success.

    Raises:
        ValueError: permanent failure (not retried, job marked `failed`).
        Exception: transient failure - retried with backoff.
    """
    start_time = time.time()

    try:
        logger.info(f"Starting embedding for {kind}: {record_id}")

        extra_fields, log_detail = await embed()

        processing_time = time.time() - start_time
        logger.info(
            f"Successfully embedded {kind} {record_id}{log_detail} in {processing_time:.2f}s"
        )
        return extra_fields, processing_time

    except ValueError as e:
        # Permanent failure. Re-raise so the job is recorded as `failed`:
        # returning success=False instead still marks the job `completed`,
        # because job status - not the payload - is what the UI reads.
        cmd_id = get_command_id(input_data)
        logger.error(f"Failed to embed {kind} {record_id} (command: {cmd_id}): {e}")
        raise
    except Exception as e:
        # Transient failure - retried with exponential-jitter backoff
        cmd_id = get_command_id(input_data)
        logger.debug(
            f"Transient error embedding {kind} {record_id} (command: {cmd_id}): {e}"
        )
        raise


async def _embed_markdown_record(
    input_data: TaskInput,
    *,
    label: str,
    record_id: str,
    loader: Callable[[str], Awaitable[Any]],
) -> Tuple[Dict[str, Any], str]:
    """
    Load a record, validate its content, embed it as markdown and UPSERT the
    embedding back onto the record. Shared by embed_note and embed_insight.
    """
    # 1. Load record
    record = await loader(record_id)
    if not record:
        raise ValueError(f"{label} '{record_id}' not found")

    if not record.content or not record.content.strip():
        raise ValueError(f"{label} '{record_id}' has no content to embed")

    # 2. Generate embedding (auto-chunks + mean pools if needed)
    # Notes and insights are typically markdown content
    cmd_id = get_command_id(input_data)
    embedding = await generate_embedding(
        record.content, content_type=ContentType.MARKDOWN, command_id=cmd_id
    )

    # 3. UPSERT embedding into the record
    await repo_query(
        "UPDATE $record_id SET embedding = $embedding",
        {
            "record_id": ensure_record_id(record_id),
            "embedding": embedding,
        },
    )

    return {}, ""


class RebuildEmbeddingsInput(TaskInput):
    mode: Literal["existing", "all"]
    include_sources: bool = True
    include_notes: bool = True
    include_insights: bool = True


class RebuildEmbeddingsOutput(TaskOutput):
    success: bool
    total_items: int
    jobs_submitted: int  # Count of embedding commands submitted
    failed_submissions: int  # Count of items that failed to submit
    sources_submitted: int = 0
    notes_submitted: int = 0
    insights_submitted: int = 0
    processing_time: float
    error_message: Optional[str] = None


class CreateInsightInput(TaskInput):
    """Input for creating a source insight with automatic retry on conflicts."""

    source_id: str
    insight_type: str
    content: str


class CreateInsightOutput(TaskOutput):
    """Output from insight creation command."""

    success: bool
    insight_id: Optional[str] = None
    processing_time: float
    error_message: Optional[str] = None


class EmbedNoteInput(TaskInput):
    """Input for embedding a single note."""

    note_id: str


class EmbedNoteOutput(TaskOutput):
    """Output from note embedding command."""

    success: bool
    note_id: str
    processing_time: float
    error_message: Optional[str] = None


class EmbedInsightInput(TaskInput):
    """Input for embedding a single source insight."""

    insight_id: str


class EmbedInsightOutput(TaskOutput):
    """Output from insight embedding command."""

    success: bool
    insight_id: str
    processing_time: float
    error_message: Optional[str] = None


class EmbedSourceInput(TaskInput):
    """Input for embedding a source (creates multiple chunk embeddings)."""

    source_id: str


class EmbedSourceOutput(TaskOutput):
    """Output from source embedding command."""

    success: bool
    source_id: str
    chunks_created: int
    processing_time: float
    error_message: Optional[str] = None


@async_task("embed_note", **EMBED_RETRY_CONFIG)
async def embed_note_command(input_data: EmbedNoteInput) -> EmbedNoteOutput:
    """
    Generate and store embedding for a single note.

    Uses the unified embedding pipeline with automatic chunking and mean pooling
    for notes that exceed the chunk size limit.

    Flow:
    1. Load Note by ID
    2. Generate embedding via generate_embedding() (auto-chunks + mean pools if needed)
    3. UPSERT note embedding in database

    Retry Strategy:
    - Retries up to 5 times for transient failures (network, timeout, etc.)
    - Uses exponential-jitter backoff (1-60s)
    - Does NOT retry permanent failures (ValueError for validation errors)
    """

    async def embed() -> Tuple[Dict[str, Any], str]:
        return await _embed_markdown_record(
            input_data,
            label="Note",
            record_id=input_data.note_id,
            loader=Note.get,
        )

    _, processing_time = await _embed_record(
        input_data,
        kind="note",
        record_id=input_data.note_id,
        embed=embed,
    )

    return EmbedNoteOutput(
        success=True,
        note_id=input_data.note_id,
        processing_time=processing_time,
    )


@async_task("embed_insight", **EMBED_RETRY_CONFIG)
async def embed_insight_command(input_data: EmbedInsightInput) -> EmbedInsightOutput:
    """
    Generate and store embedding for a single source insight.

    Uses the unified embedding pipeline with automatic chunking and mean pooling
    for insights that exceed the chunk size limit.

    Flow:
    1. Load SourceInsight by ID
    2. Generate embedding via generate_embedding() (auto-chunks + mean pools if needed)
    3. UPSERT insight embedding in database

    Retry Strategy:
    - Retries up to 5 times for transient failures (network, timeout, etc.)
    - Uses exponential-jitter backoff (1-60s)
    - Does NOT retry permanent failures (ValueError for validation errors)
    """

    async def embed() -> Tuple[Dict[str, Any], str]:
        return await _embed_markdown_record(
            input_data,
            label="Insight",
            record_id=input_data.insight_id,
            loader=SourceInsight.get,
        )

    _, processing_time = await _embed_record(
        input_data,
        kind="insight",
        record_id=input_data.insight_id,
        embed=embed,
    )

    return EmbedInsightOutput(
        success=True,
        insight_id=input_data.insight_id,
        processing_time=processing_time,
    )


@async_task("embed_source", **EMBED_RETRY_CONFIG)
async def embed_source_command(input_data: EmbedSourceInput) -> EmbedSourceOutput:
    """
    Generate and store embeddings for a source document.

    Creates multiple chunk embeddings stored in the source_embedding table.
    Uses content-type aware chunking based on file extension or content heuristics.

    Flow:
    1. Load Source by ID
    2. DELETE existing source_embedding records for this source
    3. Detect content type from file path or content
    4. Chunk text using appropriate splitter
    5. Generate embeddings for all chunks in batches
    6. Bulk INSERT source_embedding records

    Retry Strategy:
    - Retries up to 5 times for transient failures (network, timeout, etc.)
    - Uses exponential-jitter backoff (1-60s)
    - Does NOT retry permanent failures (ValueError for validation errors)
    """

    async def embed() -> Tuple[Dict[str, Any], str]:
        # 1. Load source
        source = await Source.get(input_data.source_id)
        if not source:
            raise ValueError(f"Source '{input_data.source_id}' not found")

        if not source.full_text or not source.full_text.strip():
            raise ValueError(f"Source '{input_data.source_id}' has no text to embed")

        # 2. DELETE existing embeddings (idempotency)
        logger.debug(f"Deleting existing embeddings for source {input_data.source_id}")
        await repo_query(
            "DELETE source_embedding WHERE source = $source_id",
            {"source_id": ensure_record_id(input_data.source_id)},
        )

        # 3. Detect content type from file path if available
        file_path = source.asset.file_path if source.asset else None
        content_type = detect_content_type(source.full_text, file_path)
        logger.debug(f"Detected content type: {content_type.value}")

        # 4. Chunk text using appropriate splitter
        chunks = chunk_text(source.full_text, content_type=content_type)
        total_chunks = len(chunks)

        # Log chunk statistics for debugging
        chunk_sizes = [len(c) for c in chunks]
        logger.info(
            f"Created {total_chunks} chunks for source {input_data.source_id} "
            f"(sizes: min={min(chunk_sizes) if chunk_sizes else 0}, "
            f"max={max(chunk_sizes) if chunk_sizes else 0}, "
            f"avg={sum(chunk_sizes) // len(chunk_sizes) if chunk_sizes else 0} chars)"
        )

        if total_chunks == 0:
            raise ValueError("No chunks created after splitting text")

        # 5. Generate embeddings for all chunks in batches
        cmd_id = get_command_id(input_data)
        logger.debug(f"Generating embeddings for {total_chunks} chunks")
        embeddings = await generate_embeddings(chunks, command_id=cmd_id)

        # Verify we got embeddings for all chunks
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"Embedding count mismatch: got {len(embeddings)} embeddings "
                f"for {len(chunks)} chunks"
            )

        # 6. Bulk INSERT source_embedding records
        records = [
            {
                "source": ensure_record_id(input_data.source_id),
                "order": idx,
                "content": chunk,
                "embedding": embedding,
            }
            for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings))
        ]

        logger.debug(f"Inserting {len(records)} source_embedding records")
        await repo_insert("source_embedding", records)

        return {"chunks_created": total_chunks}, f": {total_chunks} chunks"

    extra_fields, processing_time = await _embed_record(
        input_data,
        kind="source",
        record_id=input_data.source_id,
        embed=embed,
    )

    return EmbedSourceOutput(
        success=True,
        source_id=input_data.source_id,
        chunks_created=extra_fields.get("chunks_created", 0),
        processing_time=processing_time,
    )


@async_task("create_insight", **EMBED_RETRY_CONFIG)
async def create_insight_command(
    input_data: CreateInsightInput,
) -> CreateInsightOutput:
    """
    Create a source insight with automatic retry on transaction conflicts.

    This command wraps the CREATE source_insight operation with retry logic
    to handle SurrealDB transaction conflicts that occur during batch imports
    when multiple parallel transformations try to create insights concurrently.

    Flow:
    1. CREATE source_insight record in database
    2. Submit embed_insight command (fire-and-forget) for async embedding
    3. Return the insight_id

    Retry Strategy:
    - Retries up to 5 times for transient failures (network, timeout, etc.)
    - Uses exponential-jitter backoff (1-60s)
    - Does NOT retry permanent failures (ValueError for validation errors)
    """
    start_time = time.time()

    try:
        logger.info(
            f"Creating insight for source {input_data.source_id}: "
            f"type={input_data.insight_type}"
        )

        # 1. Create insight record in database
        result = await repo_query(
            """
            CREATE source_insight CONTENT {
                "source": $source_id,
                "insight_type": $insight_type,
                "content": $content
            };
            """,
            {
                "source_id": ensure_record_id(input_data.source_id),
                "insight_type": input_data.insight_type,
                "content": input_data.content,
            },
        )

        if not result or len(result) == 0:
            raise ValueError("Failed to create insight - no result returned")

        insight_id = str(result[0].get("id", ""))
        if not insight_id:
            raise ValueError("Failed to create insight - no ID in result")

        # 2. Submit embedding job (fire-and-forget)
        await submit_job("embed_insight", {"insight_id": insight_id})
        logger.debug(f"Submitted embed_insight job for {insight_id}")

        processing_time = time.time() - start_time
        logger.info(
            f"Successfully created insight {insight_id} for source "
            f"{input_data.source_id} in {processing_time:.2f}s"
        )

        return CreateInsightOutput(
            success=True,
            insight_id=insight_id,
            processing_time=processing_time,
        )

    except ValueError as e:
        # Permanent failure. Re-raise so the job is recorded as `failed`
        # (dont_autoretry_for already prevents a pointless retry).
        cmd_id = get_command_id(input_data)
        logger.error(
            f"Failed to create insight for source {input_data.source_id} "
            f"(command: {cmd_id}): {e}"
        )
        raise
    except Exception as e:
        # Transient failure - retried with exponential-jitter backoff
        cmd_id = get_command_id(input_data)
        logger.debug(
            f"Transient error creating insight for source {input_data.source_id} "
            f"(command: {cmd_id}): {e}"
        )
        raise


async def collect_items_for_rebuild(
    mode: str,
    include_sources: bool,
    include_notes: bool,
    include_insights: bool,
) -> Dict[str, List[str]]:
    """
    Collect items to rebuild based on mode and include flags.

    Returns:
        Dict with keys: 'sources', 'notes', 'insights' containing lists of item IDs
    """
    items: Dict[str, List[str]] = {"sources": [], "notes": [], "insights": []}

    if include_sources:
        if mode == "existing":
            # Query sources with embeddings (via source_embedding table)
            result = await repo_query(
                """
                RETURN array::distinct(
                    SELECT VALUE source.id
                    FROM source_embedding
                    WHERE embedding != none AND array::len(embedding) > 0
                )
                """
            )
            # RETURN returns the array directly as the result (not nested)
            if result:
                items["sources"] = [str(item) for item in result]
            else:
                items["sources"] = []
        else:  # mode == "all"
            # Query all sources with non-empty content
            result = await repo_query(
                "SELECT id FROM source WHERE full_text != none AND string::trim(full_text) != ''"
            )
            items["sources"] = [str(item["id"]) for item in result] if result else []

        logger.info(f"Collected {len(items['sources'])} sources for rebuild")

    if include_notes:
        if mode == "existing":
            # Query notes with embeddings
            result = await repo_query(
                "SELECT id FROM note WHERE embedding != none AND array::len(embedding) > 0"
            )
        else:  # mode == "all"
            # Query all notes with non-empty content
            result = await repo_query(
                "SELECT id FROM note WHERE content != none AND string::trim(content) != ''"
            )

        items["notes"] = [str(item["id"]) for item in result] if result else []
        logger.info(f"Collected {len(items['notes'])} notes for rebuild")

    if include_insights:
        if mode == "existing":
            # Query insights with embeddings
            result = await repo_query(
                "SELECT id FROM source_insight WHERE embedding != none AND array::len(embedding) > 0"
            )
        else:  # mode == "all"
            # Query all insights with non-empty content
            result = await repo_query(
                "SELECT id FROM source_insight WHERE content != none AND string::trim(content) != ''"
            )

        items["insights"] = [str(item["id"]) for item in result] if result else []
        logger.info(f"Collected {len(items['insights'])} insights for rebuild")

    return items


async def _submit_embedding_jobs(
    kind: str,
    command_name: str,
    id_field: str,
    item_ids: List[str],
    *,
    command_id: Optional[str] = None,
    done_before: int = 0,
    total_overall: int = 0,
) -> Tuple[int, int]:
    """
    Submit one embedding job per item, logging progress every 50 items.

    `done_before` / `total_overall` position this batch within the whole
    rebuild so the reported progress is across all kinds, not just this one.
    Progress is published on the same 50-item cadence as the logging - one DB
    write per item would cost more than the work being tracked.

    Returns:
        (submitted_count, failed_count)
    """
    logger.info(f"\nSubmitting {len(item_ids)} {kind} embedding jobs...")
    submitted = 0
    failed = 0
    for idx, item_id in enumerate(item_ids, 1):
        try:
            await submit_job(command_name, {id_field: item_id})
            submitted += 1
        except Exception as e:
            logger.error(f"Failed to submit {command_name} for {item_id}: {e}")
            failed += 1

        if idx % 50 == 0 or idx == len(item_ids):
            logger.info(f"  Progress: {idx}/{len(item_ids)} {kind} jobs submitted")
            await report_progress(
                command_id,
                message="queueing_embeddings",
                current=done_before + idx,
                total=total_overall,
            )

    return submitted, failed


# Coordinator task: it only fans out embed_* jobs, each of which owns its own
# retry policy. Retrying the coordinator would re-queue every item.
@async_task("rebuild_embeddings", max_retries=0)
async def rebuild_embeddings_command(
    input_data: RebuildEmbeddingsInput,
) -> RebuildEmbeddingsOutput:
    """
    Rebuild embeddings for sources, notes, and/or insights.

    This command submits individual embedding jobs for each item:
    - embed_source for sources
    - embed_note for notes
    - embed_insight for insights

    The command returns after submitting all jobs. Actual embedding
    happens asynchronously via the individual commands (which have
    their own retry strategies).

    Retry Strategy:
    - Retries disabled for this coordinator task
    - Individual embed_* tasks handle their own retries
    """
    start_time = time.time()

    try:
        logger.info("=" * 60)
        logger.info(f"Starting embedding rebuild with mode={input_data.mode}")
        logger.info(
            f"Include: sources={input_data.include_sources}, notes={input_data.include_notes}, insights={input_data.include_insights}"
        )
        logger.info("=" * 60)

        # Check embedding model availability (fail fast)
        EMBEDDING_MODEL = await model_manager.get_embedding_model()
        if not EMBEDDING_MODEL:
            raise ValueError(
                "No embedding model configured. Please configure one in the Models section."
            )

        logger.info(f"Embedding model configured: {EMBEDDING_MODEL}")

        # Collect items to process (returns IDs only)
        items = await collect_items_for_rebuild(
            input_data.mode,
            input_data.include_sources,
            input_data.include_notes,
            input_data.include_insights,
        )

        total_items = (
            len(items["sources"]) + len(items["notes"]) + len(items["insights"])
        )
        logger.info(f"Total items to rebuild: {total_items}")

        if total_items == 0:
            logger.warning("No items found to rebuild")
            return RebuildEmbeddingsOutput(
                success=True,
                total_items=0,
                jobs_submitted=0,
                failed_submissions=0,
                processing_time=time.time() - start_time,
            )

        # Submit one embedding job per item, per kind
        command_id = get_command_id(input_data)
        sources_submitted, sources_failed = await _submit_embedding_jobs(
            "source", "embed_source", "source_id", items["sources"],
            command_id=command_id, done_before=0, total_overall=total_items,
        )
        notes_submitted, notes_failed = await _submit_embedding_jobs(
            "note", "embed_note", "note_id", items["notes"],
            command_id=command_id,
            done_before=len(items["sources"]),
            total_overall=total_items,
        )
        insights_submitted, insights_failed = await _submit_embedding_jobs(
            "insight", "embed_insight", "insight_id", items["insights"],
            command_id=command_id,
            done_before=len(items["sources"]) + len(items["notes"]),
            total_overall=total_items,
        )
        failed_submissions = sources_failed + notes_failed + insights_failed

        processing_time = time.time() - start_time
        jobs_submitted = sources_submitted + notes_submitted + insights_submitted

        logger.info("=" * 60)
        logger.info("REBUILD JOBS SUBMITTED")
        logger.info(f"  Total jobs submitted: {jobs_submitted}/{total_items}")
        logger.info(f"  Sources: {sources_submitted}")
        logger.info(f"  Notes: {notes_submitted}")
        logger.info(f"  Insights: {insights_submitted}")
        logger.info(f"  Failed submissions: {failed_submissions}")
        logger.info(f"  Submission time: {processing_time:.2f}s")
        logger.info("  Note: Actual embedding happens asynchronously")
        logger.info("=" * 60)

        return RebuildEmbeddingsOutput(
            success=True,
            total_items=total_items,
            jobs_submitted=jobs_submitted,
            failed_submissions=failed_submissions,
            sources_submitted=sources_submitted,
            notes_submitted=notes_submitted,
            insights_submitted=insights_submitted,
            processing_time=processing_time,
        )

    except Exception as e:
        # Re-raise so the job is recorded as `failed`. Returning success=False
        # marks it `completed`, which showed the user a finished rebuild that
        # had in fact done nothing.
        logger.error(f"Rebuild embeddings failed: {e}")
        logger.exception(e)
        raise
