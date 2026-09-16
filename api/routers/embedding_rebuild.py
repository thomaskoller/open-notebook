from fastapi import APIRouter, HTTPException
from loguru import logger

from api.command_service import CommandService
from api.models import (
    RebuildProgress,
    RebuildRequest,
    RebuildResponse,
    RebuildStats,
    RebuildStatusResponse,
)
from open_notebook.database.repository import repo_query
from open_notebook.exceptions import OpenNotebookError

router = APIRouter()


@router.post("/rebuild", response_model=RebuildResponse)
async def start_rebuild(request: RebuildRequest):
    """
    Start a background job to rebuild embeddings.

    - **mode**: "existing" (re-embed items with embeddings) or "all" (embed everything)
    - **include_sources**: Include sources in rebuild (default: true)
    - **include_notes**: Include notes in rebuild (default: true)
    - **include_insights**: Include insights in rebuild (default: true)

    Returns command ID to track progress and estimated item count.
    """
    try:
        logger.info(f"Starting rebuild request: mode={request.mode}")

        # Estimate total items (quick count query)
        # This is a rough estimate before the command runs
        total_estimate = 0

        if request.include_sources:
            if request.mode == "existing":
                # Count sources with embeddings
                result = await repo_query(
                    """
                    SELECT VALUE count(array::distinct(
                        SELECT VALUE source.id
                        FROM source_embedding
                        WHERE embedding != none AND array::len(embedding) > 0
                    )) as count FROM {}
                    """
                )
            else:
                # Count all sources with content
                result = await repo_query(
                    "SELECT VALUE count() as count FROM source WHERE full_text != none GROUP ALL"
                )

            if result and isinstance(result[0], dict):
                total_estimate += result[0].get("count", 0)
            elif result:
                total_estimate += result[0] if isinstance(result[0], int) else 0

        if request.include_notes:
            if request.mode == "existing":
                result = await repo_query(
                    "SELECT VALUE count() as count FROM note WHERE embedding != none AND array::len(embedding) > 0 GROUP ALL"
                )
            else:
                result = await repo_query(
                    "SELECT VALUE count() as count FROM note WHERE content != none GROUP ALL"
                )

            if result and isinstance(result[0], dict):
                total_estimate += result[0].get("count", 0)
            elif result:
                total_estimate += result[0] if isinstance(result[0], int) else 0

        if request.include_insights:
            if request.mode == "existing":
                result = await repo_query(
                    "SELECT VALUE count() as count FROM source_insight WHERE embedding != none AND array::len(embedding) > 0 GROUP ALL"
                )
            else:
                result = await repo_query(
                    "SELECT VALUE count() as count FROM source_insight GROUP ALL"
                )

            if result and isinstance(result[0], dict):
                total_estimate += result[0].get("count", 0)
            elif result:
                total_estimate += result[0] if isinstance(result[0], int) else 0

        logger.info(f"Estimated {total_estimate} items to process")

        # Submit command
        command_id = await CommandService.submit_command_job(
            "open_notebook",
            "rebuild_embeddings",
            {
                "mode": request.mode,
                "include_sources": request.include_sources,
                "include_notes": request.include_notes,
                "include_insights": request.include_insights,
            },
        )

        logger.info(f"Submitted rebuild command: {command_id}")

        return RebuildResponse(
            command_id=command_id,
            total_items=total_estimate,
            message=f"Rebuild operation started. Estimated {total_estimate} items to process.",
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Failed to start rebuild: {e}")
        logger.exception(e)
        raise HTTPException(
            status_code=500, detail=f"Failed to start rebuild operation: {str(e)}"
        )


@router.get("/rebuild/{command_id}/status", response_model=RebuildStatusResponse)
async def get_rebuild_status(command_id: str):
    """
    Get the status of a rebuild operation.

    Returns:
    - **status**: queued, running, completed, failed
    - **progress**: processed count, total count, percentage
    - **stats**: breakdown by type (sources, notes, insights, failed)
    - **timestamps**: started_at, completed_at
    """
    try:
        status = await CommandService.get_command_status(command_id)

        if status["status"] == "unknown":
            raise HTTPException(status_code=404, detail="Rebuild command not found")

        response = RebuildStatusResponse(
            command_id=command_id,
            status=status["status"],
        )

        # Live progress while the job is still fanning out jobs (published by
        # the task via report_progress); the completed result supersedes it.
        live = status.get("progress")
        if isinstance(live, dict) and live.get("total"):
            total = live["total"]
            processed = live.get("current", 0)
            response.progress = RebuildProgress(
                processed=processed,
                total=total,
                percentage=round((processed / total * 100) if total > 0 else 0, 2),
            )

        result = status.get("result")
        if isinstance(result, dict):
            if "total_items" in result and "jobs_submitted" in result:
                total = result["total_items"]
                submitted = result["jobs_submitted"]
                response.progress = RebuildProgress(
                    processed=submitted,
                    total=total,
                    percentage=round((submitted / total * 100) if total > 0 else 0, 2),
                )

            response.stats = RebuildStats(
                sources=result.get("sources_submitted", 0),
                notes=result.get("notes_submitted", 0),
                insights=result.get("insights_submitted", 0),
                failed=result.get("failed_submissions", 0),
            )

        response.started_at = status.get("created")
        response.completed_at = status.get("updated")

        if status["status"] == "failed":
            response.error_message = status.get("error_message") or "Unknown error"

        return response

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Failed to get rebuild status: {e}")
        logger.exception(e)
        raise HTTPException(
            status_code=500, detail=f"Failed to get rebuild status: {str(e)}"
        )
