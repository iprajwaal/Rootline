from langchain_core.messages import HumanMessage

from rootline.agent.graph import graph


def test_graph_echoes_last_message() -> None:
    result = graph.invoke({"messages": [HumanMessage(content="hello")]})

    assert result["messages"][-1].content == "echo: hello"
