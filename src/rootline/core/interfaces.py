from collections.abc import Callable
from typing import Protocol

from rootline.core.models import Classification, ParsedDoc

# One parser per file format: raw bytes and the source key in, blocks out.
Parser = Callable[[bytes, str], ParsedDoc]


class Classifier(Protocol):
    # Rules, logistic regression and the LLM each label a document as RCA or SOP.
    def classify(self, doc: ParsedDoc) -> Classification: ...
