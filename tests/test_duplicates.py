from rootline.ingest.duplicates import containment, shingles


def words(start: int, end: int) -> str:
    return " ".join(f"word{i}" for i in range(start, end))


def test_short_copy_of_a_long_document_counts_as_contained() -> None:
    assert containment(words(100, 200), words(0, 400), set()) > 0.9


def test_unrelated_texts_share_nothing() -> None:
    assert containment(words(0, 200), words(500, 700), set()) < 0.05


def test_half_overlap_is_estimated_close_to_the_exact_share() -> None:
    assert abs(containment(words(0, 200), words(100, 300), set()) - 0.5) < 0.1


def test_shared_boilerplate_does_not_count() -> None:
    template = words(900, 960)
    first, second = f"{template} {words(0, 100)}", f"{template} {words(500, 600)}"

    assert containment(first, second, shingles(template)) < 0.05
