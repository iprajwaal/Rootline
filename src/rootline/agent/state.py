import operator
from datetime import UTC, datetime
from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from rootline.agent.guard import GuardReport
from rootline.agent.understand import Understanding


class Step(BaseModel):
    request_id: str
    node: str
    args: dict[str, str | int | float]
    detail: str
    ms: float
    llm_calls: int = 0
    # When it ran, so a drafted RCA timeline comes from real times.
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlanStep(BaseModel):
    ordinal: int
    instruction: str
    command: str = ""
    expect: str = ""
    changes: bool = False
    # What a dry run on a copy of the system showed, for steps that change it.
    tested: str = ""
    # The SOP the step comes from, so the reply can cite it.
    source: str = ""


class Focus(BaseModel):
    # What the conversation is about, so "show steps" or "explain step 2" need no ID. It is
    # the incident's memory: follow-ups and the write-up at the end build on it.
    sop: str | None = None
    sop_version: int | None = None
    rca: str | None = None
    issue: str | None = None
    plan: list[PlanStep] = []
    failing: list[str] = []
    drafts: list[int] = []
    # Rootline asked whether to write the incident up and waits for a yes or a no.
    offered: bool = False


# Only the messages come in with a turn; the nodes fill in the rest.
class AgentState(MessagesState, total=False):
    request_id: str
    guard: GuardReport
    understanding: Understanding
    trace: Annotated[list[Step], operator.add]
    focus: Focus
    # The troubleshooting agent's own conversation with the model, apart from the chat.
    work: list[AnyMessage]
