import pytest
from pydantic import ValidationError

from rootline.models import Classification, SopRecord, SopStep


def steps(*ordinals: int) -> list[SopStep]:
    return [SopStep(ordinal=n, instruction=f"step {n}") for n in ordinals]


def test_sop_record_rejects_steps_out_of_order() -> None:
    with pytest.raises(ValidationError):
        SopRecord(steps=steps(1, 3, 2))


def test_sop_record_rejects_missing_step() -> None:
    with pytest.raises(ValidationError):
        SopRecord(steps=steps(1, 2, 4))


def test_sop_record_rejects_rollback_out_of_order() -> None:
    with pytest.raises(ValidationError):
        SopRecord(steps=steps(1), rollback=steps(2, 1))


def test_sop_record_json_round_trip_keeps_step_order() -> None:
    record = SopRecord(steps=steps(1, 2, 3))

    restored = SopRecord.model_validate_json(record.model_dump_json())

    assert [step.instruction for step in restored.steps] == ["step 1", "step 2", "step 3"]


def test_classification_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        Classification(label="sop", confidence=1.2, method="rules")
