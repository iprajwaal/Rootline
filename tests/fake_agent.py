import uuid

from langchain_core.messages import AIMessage, AnyMessage

from rootline.agent.answer import Claim
from rootline.agent.troubleshoot import AskConclusion, Conclusion, StepLlm


def turn(note: str, *calls: tuple[str, dict[str, str]]) -> AIMessage:
    return AIMessage(
        content=note,
        tool_calls=[
            {"name": name, "args": args, "id": f"call-{uuid.uuid4().hex[:8]}"}
            for name, args in calls
        ],
    )


class Scripted:
    # Stands in for the tool-calling model: one list of turns per investigation, answered by
    # how far that investigation has got.
    def __init__(self, *rounds: list[AIMessage]) -> None:
        self.rounds = rounds
        self.round = 0
        self.seen: list[list[AnyMessage]] = []

    def __call__(self, messages: list[AnyMessage]) -> AIMessage:
        self.seen.append(messages)
        # A new investigation starts with only its instructions and its task.
        self.round += len(messages) == 2
        turns = self.rounds[min(self.round, len(self.rounds)) - 1] if self.rounds else []
        made = sum(isinstance(m, AIMessage) for m in messages)
        return turns[made] if made < len(turns) else AIMessage(content="Done.")


def concluding(claims: list[Claim], *steps: StepLlm) -> AskConclusion:
    def conclude(prompt: str) -> Conclusion:
        return Conclusion(claims=claims, steps=list(steps))

    return conclude
