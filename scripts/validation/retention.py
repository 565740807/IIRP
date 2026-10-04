"""Real installed database/file retention check across Compose stop/start."""

import argparse
import hashlib
import json
from pathlib import Path

from iirp.config import settings
from iirp.db import session
from iirp.models import SourceObject
from iirp.storage import save_object

from scripts.validation.identity import verified_identity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "verify"])
    args = parser.parse_args()
    identity = verified_identity()
    payload = ("Synthetic retention " + identity["validation_id"]).encode()
    digest = hashlib.sha256(payload).hexdigest()
    if args.action == "seed":
        obj = save_object(payload, "text/plain")
        with session() as s, s.begin():
            assert s.get(SourceObject, digest) is None
            s.add(SourceObject(**obj))
    with session() as s:
        row = s.get(SourceObject, digest)
        assert row and row.byte_size == len(payload)
        assert (settings().runtime_dir / Path(row.relative_path)).read_bytes() == payload
    print(json.dumps({**identity, "action": args.action, "object_sha256": digest, "pass": True}))


if __name__ == "__main__":
    main()
