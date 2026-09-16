from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from api.command_service import CommandService
from api.models import CommandJobResponse, CommandJobStatusResponse
from open_notebook.exceptions import OpenNotebookError

router = APIRouter()


class CommandExecutionRequest(BaseModel):
    command: str = Field(
        ..., description="Command function name (e.g., 'generate_podcast')"
    )
    app: str = Field(..., description="Application name (e.g., 'open_notebook')")
    input: Dict[str, Any] = Field(..., description="Arguments to pass to the command")


@router.post("/commands/jobs", response_model=CommandJobResponse)
async def execute_command(request: CommandExecutionRequest):
    """
    Submit a command for background processing.
    Returns immediately with job ID for status tracking.

    Example request:
    {
        "command": "generate_podcast",
        "app": "open_notebook",
        "input": {
            "episode_profile": "tech_experts",
            "speaker_profile": "tech_experts",
            "episode_name": "My Episode",
            "content": "Content to discuss"
        }
    }
    """
    try:
        job_id = await CommandService.submit_command_job(
            module_name=request.app,
            command_name=request.command,
            command_args=request.input,
        )

        return CommandJobResponse(
            job_id=job_id,
            status="queued",
            message=f"Command '{request.command}' submitted successfully",
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error submitting command: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to submit command")


@router.get("/commands/jobs", response_model=List[CommandJobStatusResponse])
async def list_command_jobs(
    command_filter: Optional[str] = Query(None, description="Filter by command name"),
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    active: bool = Query(
        False, description="Only jobs that are queued, running or retrying"
    ),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of jobs"),
):
    """List background jobs, newest first.

    This is what the jobs drawer polls: with `active=true` it is the whole
    in-flight picture in one request, instead of a poll per source/episode.
    """
    try:
        jobs = await CommandService.list_command_jobs(
            command_filter=command_filter,
            status_filter=status_filter,
            active_only=active,
            limit=limit,
        )
        return [CommandJobStatusResponse(**job) for job in jobs]

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error listing command jobs: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list command jobs")


@router.get("/commands/jobs/{job_id}", response_model=CommandJobStatusResponse)
async def get_command_job_status(job_id: str):
    """Get the status of a specific command job"""
    try:
        status_data = await CommandService.get_command_status(job_id)
        return CommandJobStatusResponse(**status_data)

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching job status: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch job status")


@router.delete("/commands/jobs/{job_id}")
async def cancel_command_job(job_id: str):
    """Cancel a queued job.

    Returns 409 when the job has already started or finished: the worker runs
    a threads pool, which has no process to signal, so a started task always
    runs to completion (ADR-009).
    """
    try:
        cancelled = await CommandService.cancel_command_job(job_id)
        if not cancelled:
            raise HTTPException(
                status_code=409,
                detail="Job cannot be cancelled - it has already started or finished",
            )
        return {"job_id": job_id, "cancelled": True}

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error cancelling command job: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to cancel command job")
