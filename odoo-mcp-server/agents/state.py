"""Shared state and handoff data structures for multi-agent orchestration."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepResult:
    """Record of a single worker agent's execution output."""

    step_number: int
    agent_name: str
    task: str
    output: str
    success: bool = True


@dataclass
class TaskState:
    """Shared clipboard passed across the Supervisor and specialized workers."""

    user_id: int
    chat_id: int
    original_request: str
    plan: list[dict[str, str]] = field(default_factory=list)
    completed_steps: list[StepResult] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    context_notes: dict[str, Any] = field(default_factory=dict)

    def add_artifact(self, path: str) -> None:
        """Register a generated file path (e.g., '/tmp/INV_1.pdf') without duplicates."""
        if path and path not in self.artifacts:
            self.artifacts.append(path)

    def format_handoff_context(self) -> str:
        """Render prior worker findings and file paths for the next worker."""
        if not self.completed_steps and not self.artifacts:
            return "No prior steps executed yet."

        lines: list[str] = []
        for step in self.completed_steps:
            status = "OK" if step.success else "FAILED"
            lines.append(
                f"- Step {step.step_number} [{step.agent_name.upper()} - {status}]: "
                f"{step.output}"
            )

        if self.artifacts:
            lines.append(f"- Available local file artifacts: {', '.join(self.artifacts)}")

        return "\n".join(lines)