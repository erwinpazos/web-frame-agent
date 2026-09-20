import asyncio
import json
from fastapi import APIRouter, HTTPException, status, Depends, Request
from fastapi.responses import StreamingResponse
from app.models.agent import TaskRequest, TaskStatusResponse
from app.services.agent_service import agent_service
from app.core.logger import logger
from app.core.security import authenticate_http_request, rate_limit_task_launch
from app.core.config import settings

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.get(
    "/status",
    response_model=TaskStatusResponse,
    summary="Get current agent task status",
    dependencies=[Depends(authenticate_http_request)],
)
async def get_agent_status():
    """Returns the current agent status."""
    raw_status = agent_service.get_status()
    is_running = raw_status.get("is_running", False)
    return TaskStatusResponse(
        task_id=raw_status.get("active_task_id") or "none",
        status="running" if is_running else "idle",
        current_step=0,
        max_steps=15,
        message="Agent is running" if is_running else "Agent is idle",
    )


@router.post(
    "/run",
    response_model=TaskStatusResponse,
    summary="Trigger a browser-use agent task",
    dependencies=[Depends(authenticate_http_request), Depends(rate_limit_task_launch)],
)
async def run_agent_task(request: TaskRequest):
    """Starts an asynchronous browser navigation task."""
    try:
        task_id = await agent_service.start_task(
            prompt=request.task,
            start_url=request.start_url or settings.default_target_url,
            session_id=request.session_id,
            max_steps=request.max_steps,
            headless=request.headless,
        )
        return TaskStatusResponse(
            task_id=task_id,
            status="running",
            current_step=0,
            max_steps=request.max_steps,
            message="Task started successfully",
        )
    except RuntimeError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    except Exception as exc:
        logger.error(f"Error starting agent task: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start task: {str(exc)}",
        )


@router.post(
    "/stop",
    summary="Stop currently active agent task",
    dependencies=[Depends(authenticate_http_request)],
)
async def stop_agent_task():
    """Stops the active task."""
    stopped = await agent_service.stop_task()
    return {
        "success": stopped,
        "message": "Task stopped" if stopped else "No active task to stop",
    }
@router.get(
    "/events",
    summary="Server-Sent Events (SSE) stream for agent execution and text chunks",
    dependencies=[Depends(authenticate_http_request)],
)
async def agent_events_sse(request: Request):
    """Streams agent lifecycle and text chunks via Server-Sent Events (text/event-stream)."""
    queue = agent_service.subscribe()

    async def event_generator():
        try:
            # Yield initial connection heartbeat
            yield f"event: ping\ndata: {json.dumps({'status': 'connected'})}\n\n"

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_type = event_data.get("type", "message")
                    yield f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive SSE comment
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            agent_service.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
