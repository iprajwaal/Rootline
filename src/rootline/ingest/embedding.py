from pathlib import Path

from fastembed import TextEmbedding


class Embedder:
    def __init__(self, model: str, cache_dir: Path) -> None:
        self.model_name = model
        self.model = TextEmbedding(model, cache_dir=str(cache_dir))

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.embed(texts)]

    def query(self, text: str) -> list[float]:
        # bge models expect a search instruction on queries; query_embed adds it.
        return [float(x) for x in next(iter(self.model.query_embed([text])))]


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
