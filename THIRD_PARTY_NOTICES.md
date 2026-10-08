# Third-party notices

IIRP's own source code is released under the MIT License (see `LICENSE`). The
license applies to this project's code only; third-party components keep their own
licenses.

Exact Python and npm versions are in `uv.lock` and `frontend/package-lock.json`.

- `docs/THIRD_PARTY_INVENTORY.json` lists every locked package with its declared
  license.
- `docs/THIRD_PARTY_REVIEW.json` records, for each locked distribution (one selected
  Python artifact per version; every npm artifact, including optional platform
  packages), the download URL, the verified hash and the notice files it contains.
- `docs/THIRD_PARTY_LICENSE_TEXTS.txt` keeps the LICENSE / NOTICE / COPYING texts
  found in those archives, with archive paths and hashes.

After a dependency change, refresh them with:

```sh
python3 scripts/third_party_review.py
python3 scripts/dependency_inventory.py > docs/THIRD_PARTY_INVENTORY.json
```

Known gaps: `react-remove-scroll-bar`, `uri-js-replace` and
`@napi-rs/lzma-linux-x64-gnu` ship without a notice file at the locked version
(their declared license, MIT, is recorded). The platform-specific esbuild and Rollup
archives omit notice bodies; their parent packages' notices are retained. Bundled
native components inside Python wheels are not reviewed separately.

Container images are pinned by digest in `deploy/Dockerfile`, `deploy/compose.yaml`
and `deploy/image-lock.json`. Debian and PGDG packages installed while building the
image are not snapshot-pinned. PostgreSQL is distributed under the
[PostgreSQL License](https://www.postgresql.org/about/licence/).
