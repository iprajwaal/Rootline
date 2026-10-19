from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from rootline.config import Settings
from rootline.llm.tracing import tracing_callbacks


def echo(state: MessagesState) -> dict[str, list[AIMessage]]:
    last = state["messages"][-1]
    return {"messages": [AIMessage(content=f"echo: {last.text}")]}


builder = StateGraph(MessagesState)
builder.add_node("echo", echo)
builder.add_edge(START, "echo")
builder.add_edge("echo", END)
# With Langfuse keys set, every node run and the LLM calls inside it land in one trace.
graph = builder.compile().with_config(callbacks=tracing_callbacks(Settings()))
