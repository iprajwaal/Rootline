from rootline.eval.runner import Row, mean_ci, summarize


def retrieved(id_: str, lane: str, answerable: bool, hit: bool, abstained: bool) -> Row:
    return {
        "id": id_, "kind": "error", "origin": "greenfield", "lane": lane,
        "answerable": answerable, "hit1": hit, "rr": 1.0 if hit else 0.0, "recall5": hit,
        "wrong_type": False, "abstained": abstained, "noisy_hit1": hit,
    }  # fmt: skip


def test_bootstrap_interval_is_repeatable_and_holds_the_mean() -> None:
    values = [1, 1, 0, 1, 0, 1, 1, 1]

    first, second = mean_ci(values), mean_ci(values)

    assert first == second
    assert first["low"] <= first["mean"] <= first["high"]  # type: ignore[operator]


def test_abstain_precision_and_recall_count_the_right_cases() -> None:
    rows = {
        "retrieval": [
            retrieved("a", "rca", answerable=True, hit=True, abstained=False),
            retrieved("b", "rca", answerable=False, hit=False, abstained=True),
            retrieved("c", "rca", answerable=False, hit=False, abstained=False),
            retrieved("d", "rca", answerable=True, hit=False, abstained=True),
        ],
        "intent": [],
        "graph": [],
    }

    metrics = summarize(rows)

    assert metrics["abstain"] == {
        "precision": 0.5, "recall": 0.5, "should_abstain": 2, "abstained": 2,
    }  # fmt: skip
    assert metrics["retrieval"]["rca/greenfield"]["exact@1"]["mean"] == 0.5
