from rootline.agent.factory import default_graph
from rootline.config import Settings
from rootline.core.logs import configure_logging

# Loaded by the LangGraph server through langgraph.json.
settings = Settings()
configure_logging(settings.log_level)
graph = default_graph(settings)
