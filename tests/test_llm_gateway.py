import copy
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler, CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langfuse.langchain import CallbackHandler
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel

from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.ingest.classify import LlmVerdict
from rootline.ingest.extract import Record
from rootline.ingest.factory import label_asker, record_asker
from rootline.ingest.store import Store
from rootline.llm import structured
from rootline.llm.ledger import Call, PostgresLedger, Prices
from rootline.llm.structured import LLMUnavailable, StructuredLLM, build_llm
from rootline.llm.tracing import mask, tracing_callbacks

CORPUS = Path(__file__).parents[1] / "corpus"
PRICES = Prices(input_per_mtok=0.30, output_per_mtok=2.50)
USAGE: UsageMetadata = {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}
VERDICT = LlmVerdict(label="sop", confidence=0.9, reason="numbered steps and commands")


class JsonModel(GenericFakeChatModel):
    # Stands in for Gemini: replies with JSON and reports token usage the same way.
    def with_structured_output(
        self, schema: dict[str, Any] | type, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[LanguageModelInput, dict[str, Any] | BaseModel]:
        assert isinstance(schema, type) and issubclass(schema, BaseModel)
        parse: RunnableLambda[BaseMessage, BaseModel] = RunnableLambda(
            lambda message: schema.model_validate_json(message.text)
        )
        return self | parse


class TimingOut(JsonModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise TimeoutError("deadline exceeded")


class Recorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.runs: dict[UUID, tuple[str, UUID | None]] = {}
        self.model_starts = 0

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.runs[run_id] = (kwargs.get("name", ""), parent_run_id)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.model_starts += 1


def fake_gemini(monkeypatch: pytest.MonkeyPatch, model: type[JsonModel]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def init(name: str, **kwargs: Any) -> JsonModel:
        seen.append(kwargs)
        reply = AIMessage(content=VERDICT.model_dump_json(), usage_metadata=USAGE)
        return model(messages=iter([reply]), callbacks=kwargs["callbacks"])

    monkeypatch.setattr(structured, "init_chat_model", init)
    return seen


def gateway(
    tmp_path: Path,
    calls: list[Call],
    api_key: str = "test-key",
    tracing: tuple[BaseCallbackHandler, ...] = (),
) -> StructuredLLM:
    return StructuredLLM(
        model="test-model",
        api_key=api_key,
        cache_dir=tmp_path,
        record=calls.append,
        prices=PRICES,
        timeout_s=5.0,
        max_retries=1,
        tracing=tracing,
    )


def ask(llm: StructuredLLM, prompt: str = "a prompt") -> LlmVerdict:
    return llm.ask("classify", prompt, LlmVerdict, "classify-v1")


def test_model_call_writes_one_ledger_row_with_tokens_cost_and_prompt_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_gemini(monkeypatch, JsonModel)
    calls: list[Call] = []

    assert ask(gateway(tmp_path, calls)) == VERDICT
    [call] = calls
    assert (call.kind, call.task, call.prompt_version, call.model) == (
        "llm_call",
        "classify",
        "classify-v1",
        "test-model",
    )
    assert (call.input_tokens, call.output_tokens) == (1200, 300)
    assert call.cost_usd == pytest.approx(0.00111)


def test_model_is_built_with_the_timeout_and_retry_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = fake_gemini(monkeypatch, JsonModel)

    ask(gateway(tmp_path, []))

    assert (seen[0]["timeout"], seen[0]["max_retries"]) == (5.0, 1)


def test_vertex_ai_key_sends_calls_through_vertex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = fake_gemini(monkeypatch, JsonModel)

    ask(replace(gateway(tmp_path, []), vertexai=True))

    assert seen[0]["vertexai"] is True


def test_answer_is_cached_and_reused_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_gemini(monkeypatch, JsonModel)
    calls: list[Call] = []
    ask(gateway(tmp_path, calls))
    # The key format must not change, or the committed cache stops matching.
    key = hashlib.sha256(b"classify\ntest-model\na prompt").hexdigest()

    assert (tmp_path / "classify" / f"{key}.json").exists()
    assert ask(gateway(tmp_path, calls, api_key="")) == VERDICT
    assert [(call.kind, call.cost_usd) for call in calls] == [
        ("llm_call", pytest.approx(0.00111)),
        ("llm_cache", 0.0),
    ]


def test_missing_key_raises_unavailable_and_writes_one_row(tmp_path: Path) -> None:
    calls: list[Call] = []

    with pytest.raises(LLMUnavailable):
        ask(gateway(tmp_path, calls, api_key=""), "a prompt nobody cached")

    assert [(call.kind, call.degraded) for call in calls] == [("llm_unavailable", ("no_api_key",))]


def test_timed_out_call_raises_unavailable_and_writes_one_error_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_gemini(monkeypatch, TimingOut)
    calls: list[Call] = []

    with pytest.raises(LLMUnavailable, match="deadline exceeded"):
        ask(gateway(tmp_path, calls))

    assert [(call.kind, call.degraded) for call in calls] == [("llm_error", ("TimeoutError",))]


def test_ledger_rows_are_stored_in_postgres(migrated_db: psycopg.Connection, db_url: str) -> None:
    call = Call(
        request_id="req-1",
        kind="llm_call",
        task="classify",
        model="test-model",
        prompt_version="classify-v1",
        latency_ms=420,
        input_tokens=1200,
        output_tokens=300,
        cost_usd=0.00111,
    )
    ledger = PostgresLedger(db_url)

    ledger.record(call)
    ledger.record(replace(call, kind="llm_unavailable", degraded=("no_api_key",)))

    rows = migrated_db.execute(
        "select kind, prompt_version, input_tokens, output_tokens, cost_usd, latency_ms, degraded"
        " from ledger order by id"
    ).fetchall()
    assert rows == [
        ("llm_call", "classify-v1", 1200, 300, Decimal("0.001110"), 420, []),
        ("llm_unavailable", "classify-v1", 1200, 300, Decimal("0.001110"), 420, ["no_api_key"]),
    ]


def test_ledger_write_failure_is_logged_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call = Call("req-1", "llm_cache", "classify", "test-model", "classify-v1", latency_ms=1)

    PostgresLedger("postgresql://nobody@127.0.0.1:1/none").record(call)

    assert "ledger write failed task=classify" in caplog.text


def recording_analyzer(
    analyzer: Analyzer, settings: Settings, calls: list[Call], asked: list[str]
) -> Analyzer:
    llm = build_llm(settings, calls.append)
    label, record = label_asker(llm), record_asker(llm)

    def ask_label(prompt: str) -> LlmVerdict:
        asked.append("classify")
        return label(prompt)

    def ask_record(prompt: str, kind: Literal["sop", "rca"]) -> Record:
        asked.append(f"extract_{kind}")
        return record(prompt, kind)

    return replace(analyzer, ask_label=ask_label, ask_record=ask_record)


def test_ingest_without_a_key_runs_and_writes_one_row_per_llm_call(
    migrated_db: psycopg.Connection,
    analyzer: Analyzer,
    offline_settings: Settings,
    tmp_path: Path,
) -> None:
    calls: list[Call] = []
    asked: list[str] = []
    recording = recording_analyzer(analyzer, offline_settings, calls, asked)
    store = Store(migrated_db, recording, tmp_path / "raw", offline_settings.embed_model)
    files = ["month-end-oom.eml", "worker-oom-quick-fix.txt", "db-pool-incident-and-fix.html"]

    outcomes = [
        store.ingest(f"corpus/brownfield/{name}", (CORPUS / "brownfield" / name).read_bytes())
        for name in files
    ]

    assert all(outcome.action == "created" for outcome in outcomes)
    assert asked and [call.task for call in calls] == asked
    assert {call.kind for call in calls} == {"llm_cache"}
    assert {call.prompt_version for call in calls} <= {"classify-v1", "extract-v1"}


def test_llm_calls_in_one_api_request_share_its_request_id(
    migrated_db: psycopg.Connection,
    analyzer: Analyzer,
    offline_settings: Settings,
    tmp_path: Path,
) -> None:
    calls: list[Call] = []
    recording = recording_analyzer(analyzer, offline_settings, calls, [])
    settings = offline_settings.model_copy(update={"raw_dir": tmp_path / "raw"})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: recording
    content = (CORPUS / "brownfield" / "worker-oom-quick-fix.txt").read_bytes()

    response = TestClient(app).post(
        "/documents", files={"file": ("fix.txt", content)}, headers={"x-request-id": "req-p16"}
    )
    app.dependency_overrides.clear()

    assert response.headers["x-request-id"] == "req-p16"
    assert calls and {call.request_id for call in calls} == {"req-p16"}


def test_tracing_is_off_without_langfuse_keys() -> None:
    assert tracing_callbacks(Settings(langfuse_public_key="", langfuse_secret_key="")) == []


def test_tracing_reuses_one_langfuse_handler_per_process() -> None:
    settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_base_url="http://127.0.0.1:1",
    )

    [handler] = tracing_callbacks(settings)

    assert isinstance(handler, CallbackHandler)
    assert tracing_callbacks(settings) == [handler]


def test_traced_graph_config_survives_the_servers_deep_copy() -> None:
    settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_base_url="http://127.0.0.1:1",
    )
    config = {"callbacks": tracing_callbacks(settings)}

    assert copy.deepcopy(config)["callbacks"][0] is config["callbacks"][0]


def test_traced_data_is_redacted_before_it_is_sent() -> None:
    data = {
        "messages": [HumanMessage(content="deploy failed with key AKIAABCDEFGHIJKLMNOP")],
        "notes": ("token ghp_" + "a1" * 18,),
        "count": 3,
    }

    masked = json.dumps(mask(data=data))

    assert "AKIAABCDEFGHIJKLMNOP" not in masked and "ghp_a1" not in masked
    assert "[REDACTED:AWS_ACCESS_KEY]" in masked and '"count": 3' in masked


def test_call_outside_a_graph_is_traced_under_its_task_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_gemini(monkeypatch, JsonModel)
    recorder = Recorder()

    ask(gateway(tmp_path, [], tracing=(recorder,)))

    assert ("classify", None) in recorder.runs.values()
    assert recorder.model_starts == 1


def test_call_inside_a_graph_node_nests_under_the_node_and_is_traced_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_gemini(monkeypatch, JsonModel)
    recorder = Recorder()
    llm = gateway(tmp_path, [], tracing=(recorder,))

    def understand(state: MessagesState) -> dict[str, Any]:
        ask(llm)
        return {}

    builder = StateGraph(MessagesState)
    builder.add_node("understand", understand)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", END)
    graph = builder.compile().with_config(callbacks=[recorder])

    graph.invoke({"messages": [HumanMessage(content="orders-api is slow")]})

    names = {run_id: name for run_id, (name, _) in recorder.runs.items()}
    [parent] = [parent for name, parent in recorder.runs.values() if name == "classify"]
    assert parent and names[parent] == "understand"
    assert recorder.model_starts == 1
