from typing import Optional
from pydantic import BaseModel, Field


class TaskRequest(BaseModel):
    task: str = Field(..., description="Natural language instructions for the navigation agent")
    start_url: Optional[str] = Field(default="https://www.google.com", description="Initial URL to navigate")
    session_id: Optional[str] = Field(default=None, description="Conversation session ID for Langfuse grouping")
    max_steps: int = Field(default=25, ge=1, le=100, description="Maximum number of agent reasoning steps")
    headless: bool = Field(default=False, description="Run browser in headless or headed mode")

class TaskStatusResponse(BaseModel):
    task_id: str
    status: str = Field(default="idle", description="Current status: idle, running, completed, failed, stopped")
    current_step: int = 0
    max_steps: int = 25
    message: Optional[str] = None
