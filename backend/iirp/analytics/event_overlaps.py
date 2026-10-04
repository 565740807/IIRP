"""Bounded representation of inclusive, frozen D-5..D+5 interval relations.

Two persistent count trees index row positions by sorted start/end dates. For
[a,b], starts <= b minus ends < a is precisely its intersecting set. Reporting
the first K positions traverses only nonempty difference subtrees; no pair list
is ever built. Build O(n log n) time/space, all summaries O(n K log n) time and
O(n K) output, including the fully dense worst case. K is a fixed constant.
"""

from bisect import bisect_left, bisect_right

REPRESENTATION_VERSION = "event-overlaps-v2"
PREVIEW_LIMIT = 10
ORDERING = "frozen_row_order"


def summarize_overlaps(rows):
    size = len(rows)
    intervals = [
        (i, row["points"][0]["date"], row["points"][-1]["date"])
        for i, row in enumerate(rows)
        if row.get("points")
    ]
    # Node zero is the shared empty subtree. Each insertion copies its path.
    left, right, count = [0], [0], [0]

    def insert(node, lo, hi, position):
        new = len(count)
        left.append(left[node])
        right.append(right[node])
        count.append(count[node] + 1)
        if hi - lo > 1:
            mid = (lo + hi) // 2
            if position < mid:
                left[new] = insert(left[node], lo, mid, position)
            else:
                right[new] = insert(right[node], mid, hi, position)
        return new

    def index(endpoint):
        dates, roots = [], [0]
        for item in sorted(intervals, key=lambda item: (item[endpoint], item[0])):
            dates.append(item[endpoint])
            roots.append(insert(roots[-1], 0, size, item[0]))
        return dates, roots

    starts, start_roots = index(1)
    ends, end_roots = index(2)

    def first(a, b, lo, hi, excluded, output):
        if count[a] == count[b] or len(output) == PREVIEW_LIMIT:
            return
        if hi - lo == 1:
            if lo != excluded:
                output.append(rows[lo]["key"])
            return
        mid = (lo + hi) // 2
        first(left[a], left[b], lo, mid, excluded, output)
        first(right[a], right[b], mid, hi, excluded, output)

    for row in rows:
        row["overlap"] = {
            "total": 0,
            "preview_event_ids": [],
            "preview_limit": PREVIEW_LIMIT,
            "truncated": False,
            "ordering": ORDERING,
        }
    for i, start, end in intervals:
        a = start_roots[bisect_right(starts, end)]
        b = end_roots[bisect_left(ends, start)]
        summary = rows[i]["overlap"]
        summary["total"] = count[a] - count[b] - 1
        first(a, b, 0, size, i, summary["preview_event_ids"])
        summary["truncated"] = summary["total"] > len(summary["preview_event_ids"])
