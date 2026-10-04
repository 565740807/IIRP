"""Independent interval oracle, including endpoints, missing windows and order."""

import random

from iirp.analytics.event_overlaps import PREVIEW_LIMIT, summarize_overlaps


def oracle(rows, row):
    if not row["points"]:
        return []
    a, b = row["points"][0]["date"], row["points"][-1]["date"]
    return [
        other["key"]
        for other in rows
        if other is not row
        and other["points"]
        and other["points"][0]["date"] <= b
        and other["points"][-1]["date"] >= a
    ]


def check(intervals):
    rows = [
        dict(key=str(i), points=[] if span is None else [{"date": span[0]}, {"date": span[1]}])
        for i, span in enumerate(intervals)
    ]
    expected = [oracle(rows, row) for row in rows]
    summarize_overlaps(rows)
    for row, ids in zip(rows, expected, strict=True):
        assert row["overlap"] == dict(
            total=len(ids),
            preview_event_ids=ids[:PREVIEW_LIMIT],
            preview_limit=10,
            truncated=len(ids) > 10,
            ordering="frozen_row_order",
        )
        assert "overlapping_event_ids" not in row


def test_deterministic_overlap_boundaries():
    check([])
    check([None])
    check([(1, 3), (3, 5), (1, 3), (2, 2), (0, 9), (10, 11), None, (3, 3)])
    check([(1, 1)] * 1000)


def test_random_intervals_match_pairwise_oracle():
    rng = random.Random(2910)
    for _ in range(25):
        check(
            [
                tuple(sorted([rng.randrange(100), rng.randrange(100)]))
                if rng.random() > 0.1
                else None
                for _ in range(130)
            ]
        )
