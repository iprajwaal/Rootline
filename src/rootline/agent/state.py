import operator
from typing import Annotated

from langgraph.graph import MessagesState
from pydantic import BaseModel

from rootline.agent.guard import GuardReport
from rootline.agent.understand import Understanding


class Step(BaseModel):
    request_id: str
    node: str
    args: dict[str, str | int | float]
    detail: str
    ms: float
    llm_calls: int = 0


class Focus(BaseModel):
    # The documents the conversation is about, so "show steps" or "run step 4" need no ID.
    sop: str | None = None
    sop_version: int | None = None
    rca: str | None = None


# Only the messages come in with a turn; the nodes fill in the rest.
class AgentState(MessagesState, total=False):
    request_id: str
    guard: GuardReport
    understanding: Understanding
    trace: Annotated[list[Step], operator.add]
    focus: Focus
