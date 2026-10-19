import hashlib
from dataclasses import dataclass
from pathlib import Path

from langchain.chat_models import init_chat_model
from pydantic import BaseModel


class LLMUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class StructuredLLM:
    model: str
    api_key: str
    cache_dir: Path

    def ask[T: BaseModel](self, task: str, prompt: str, schema: type[T]) -> T:
        # Answers are cached by task, model and prompt. The cache is committed, so a run
        # without an API key gives the same results as the run that filled it.
        key = hashlib.sha256(f"{task}\n{self.model}\n{prompt}".encode()).hexdigest()
        path = self.cache_dir / task / f"{key}.json"
        if path.exists():
            return schema.model_validate_json(path.read_text())
        if not self.api_key:
            raise LLMUnavailable("no API key set")

        model = init_chat_model(self.model, model_provider="google_genai", api_key=self.api_key)
        try:
            result = model.with_structured_output(schema).invoke(prompt)
        except Exception as error:
            raise LLMUnavailable(f"{self.model} call failed: {error}") from error
        if not isinstance(result, schema):
            raise LLMUnavailable(f"{self.model} returned {type(result).__name__}")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2) + "\n")
        return result
