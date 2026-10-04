"""Real worker lifecycle/queue/fences with a test-only claim allowlist.

No supplier adapter is reachable. Computation, storage, scheduling, ownership,
heartbeats, stop/drain and publication use the unchanged production worker.
"""

from scripts.validation.identity import disable_sources, verified_identity


def main():
    verified_identity()
    disable_sources()
    from iirp import worker

    original = worker.claim
    allowed = {"fixture_check", "research_compute", "event_compute"}

    def claim(kinds=None, **kwargs):
        selected = allowed if kinds is None else allowed.intersection(kinds)
        return original(selected, **kwargs) if selected else None

    worker.claim = claim
    worker.main()


if __name__ == "__main__":
    main()
