import operator
from typing import Annotated

from langgraph.graph import MessagesState
from pydantic import BaseModel

from rootline.agent.guard import GuardReport
from rootline.agent.understand import Understanding


class Step(BaseModel):
    node: str
    detail: str
    ms: float
    llm_calls: int = 0


# Only the messages come in with a turn; the nodes fill in the rest.
class AgentState(MessagesState, total=False):
    request_id: str
    guard: GuardReport
    understanding: Understanding
    trace: Annotated[list[Step], operator.add]
