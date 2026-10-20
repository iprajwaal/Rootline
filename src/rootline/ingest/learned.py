import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from scipy.sparse import csr_matrix, hstack, spmatrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from rootline.core.models import Classification, ParsedDoc
from rootline.ingest.classify import STEP_MARKER, TIMESTAMP, procedure_share, score_blocks
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc


@dataclass(frozen=True)
class Example:
    source: str
    doc: ParsedDoc
    label: Literal["rca", "sop"]
    # Copies of one document share a group, so a copy is never judged by a model that saw it.
    group: str


@dataclass(frozen=True)
class LogisticClassifier:
    vectorizer: TfidfVectorizer
    model: LogisticRegression

    def classify(self, doc: ParsedDoc) -> Classification:
        sop = list(self.model.classes_).index("sop")
        p_sop = float(self.model.predict_proba(self.features([doc]))[0][sop])
        return Classification(
            label="sop" if p_sop >= 0.5 else "rca",
            confidence=max(p_sop, 1 - p_sop),
            method="lr",
            evidence=[f"p(sop) {p_sop:.2f} from words and rule scores"],
        )

    def features(self, docs: list[ParsedDoc]) -> spmatrix:
        words = self.vectorizer.transform([doc.text for doc in docs])
        return hstack([words, csr_matrix([rule_features(doc) for doc in docs])]).tocsr()


def train(examples: list[Example]) -> LogisticClassifier:
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)
    vectorizer.fit([example.doc.text for example in examples])
    learned = LogisticClassifier(
        vectorizer, LogisticRegression(max_iter=1000, class_weight="balanced")
    )
    features = learned.features([example.doc for example in examples])
    learned.model.fit(features, [example.label for example in examples])
    return learned


def rule_features(doc: ParsedDoc) -> list[float]:
    share, _ = procedure_share(score_blocks(doc))
    lines = doc.text.splitlines() or [""]
    steps = sum(bool(STEP_MARKER.match(line)) for line in lines) / len(lines)
    stamps = len(TIMESTAMP.findall(doc.text)) / len(lines)
    return [share, steps, stamps]


def labelled_examples(
    corpus: Path, ground_truth: tuple[str, ...] = ("brownfield",)
) -> list[Example]:
    # Mixed documents are left out: the rules find those by their layout. The hard set stays
    # out of training by default, so it keeps measuring what an unseen document gets.
    rows = [
        {"file": f"greenfield/{path.name}", "doc_type": path.name[:3], "group": path.stem.upper()}
        for path in sorted((corpus / "greenfield").iterdir())
    ]
    for name in ground_truth:
        for line in (corpus / "ground_truth" / f"{name}.jsonl").read_text().splitlines():
            row = json.loads(line)
            rows.append(row | {"group": "+".join(sorted(row.get("source_ids", [row["file"]])))})
    examples = []
    for row in rows:
        if row["doc_type"] in ("rca", "sop"):
            doc, _ = redact_doc(parse_file(corpus / row["file"]))
            label = cast(Literal["rca", "sop"], row["doc_type"])
            examples.append(Example(row["file"], doc, label, row["group"]))
    return examples
