from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph


def echo(state: MessagesState) -> dict[str, list[AIMessage]]:
    last = state["messages"][-1]
    return {"messages": [AIMessage(content=f"echo: {last.text}")]}


builder = StateGraph(MessagesState)
builder.add_node("echo", echo)
builder.add_edge(START, "echo")
builder.add_edge("echo", END)
graph = builder.compile()
