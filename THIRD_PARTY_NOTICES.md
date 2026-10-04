# Third-party notices

IIRP's own source code is released under the MIT License (see `LICENSE`). The
license applies to this project's code only; third-party components keep their own
licenses, listed below. The MIT license is not publication approval for any
third-party material.

Exact Python and npm versions are in `uv.lock`, `frontend/package-lock.json`,
and `browser/package-lock.json`. `docs/THIRD_PARTY_INVENTORY.json` enumerates them;
regenerate it with `python3 scripts/dependency_inventory.py`.
`docs/THIRD_PARTY_REVIEW.json` records integrity verification of 277 distinct
locked distributions (one selected Python artifact per version and all locked
npm artifacts, including optional platforms). Included upstream LICENSE / NOTICE
material is retained in `docs/THIRD_PARTY_LICENSE_TEXTS.txt`, with artifact URLs,
archive paths and original content hashes. Package metadata is recorded as declared by each package.

The platform-specific esbuild and Rollup archives omit notice bodies; their
matching parent-package notices are retained. Exact-version notice bodies remain
unresolved for `react-remove-scroll-bar`, `uri-js-replace` and
`@napi-rs/lzma-linux-x64-gnu`; declared MIT metadata is recorded but is not a
substitute for those missing materials. This inspection does not establish all
bundled native-component obligations or clearance for distributing browser / OS
images. Retain and review the notices for the actual chosen distribution format.

Playwright Core 1.62.1 is declared Apache-2.0 by its package metadata. This work
ports this project's existing browser tests and uses Playwright's public API; it
does not copy Playwright implementation code. Chromium and its bundled third
party components have separate notices distributed with the downloaded browser.

Container image versions and digests remain in `deploy/image-lock.json` and the
Dockerfiles. Debian/PGDG packages installed during image construction are not
snapshot-pinned; image build logs and the installed package inventory must be
retained before claiming binary reproducibility. The Playwright tarball has been
downloaded, its registry SHA-512 verified, and its integrity added to the browser
lockfile. The package's bundled browser revision/version matches browser-lock;
actual browser installation and execution are separate runtime checks.

Official upstream references: [Playwright](https://github.com/microsoft/playwright/tree/v1.62.1),
[Chromium licensing](https://www.chromium.org/chromium-os/licenses/),
[PostgreSQL license](https://www.postgresql.org/about/licence/).
