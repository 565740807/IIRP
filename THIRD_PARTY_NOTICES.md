# Third-party notices

IIRP's own source code is released under the MIT License (see `LICENSE`). The license
covers this project's code only. IIRP does not bundle third-party code in this
repository: dependencies are downloaded from PyPI, npm and container registries when you build
it, and each keeps its own license. Exact versions are locked in `uv.lock` and
`frontend/package-lock.json`.

## Direct dependencies

| Component | License |
|---|---|
| FastAPI, SQLAlchemy, Alembic, pydantic-settings, curl_cffi | MIT |
| Uvicorn, HTTPX | BSD-3-Clause |
| pdfplumber, pdfminer.six, Beautiful Soup | MIT |
| pytesseract, Tesseract OCR and English trained data | Apache-2.0 |
| pypdfium2 | Apache-2.0 OR BSD-3-Clause |
| PDFium (bundled by pypdfium2) | BSD-3-Clause, with bundled dependency notices |
| Pillow | HPND |
| psycopg | LGPL-3.0-only |
| defusedxml | PSF License |
| yfinance, exchange_calendars | Apache-2.0 |
| React, React DOM, React Router, TanStack Query / Table / Virtual, i18next, react-i18next, i18next-resources-to-backend, Motion, NumberFlow, openapi-fetch, Radix UI, clsx, tailwind-merge, tw-animate-css | MIT |
| Apache ECharts, class-variance-authority | Apache-2.0 |
| Lucide icons | ISC |
| Geist font (@fontsource-variable/geist) | SIL Open Font License 1.1 |
| UI components adapted from [shadcn/ui](https://github.com/shadcn-ui/ui) | MIT |

Transitive dependencies (for example pandas and NumPy, pulled in by yfinance) keep
their own licenses. To list everything that is installed, you can use tools such as
[pip-licenses](https://github.com/raimon49/pip-licenses) for Python and
[license-checker](https://github.com/davglass/license-checker) for npm.

## Browser testing tools

Playwright and Freezegun are development dependencies under Apache-2.0. They run
only in the disposable browser test stack and are locked in the same npm and uv
lockfiles.

## Container images

The images are pinned by digest in `deploy/Dockerfile` and `deploy/compose.yaml`:
the official Python, Node.js and PostgreSQL images. Browser tests use the official
Playwright image pinned in `deploy/playwright-image.txt`. PostgreSQL is distributed under
the [PostgreSQL License](https://www.postgresql.org/about/licence/). Debian and PGDG
packages installed while building the image are not snapshot-pinned. Tesseract and
libtesseract are pinned to Debian 5.3.0-2, and English and orientation trained data to 1:4.1.0-2.
PDFium binary distributions retain their bundled third-party license notices.

## Data sources

House Clerk PTR and OGE/White House 278-T files are public government disclosures.
Senate disclosure access requires accepting the official personal-research use
restrictions; the collector uses an ordinary contact-bearing session and does not
bypass access protection. Public fixture excerpts cite their original files.

SEC EDGAR data is public. Prices come through yfinance, which reads Yahoo Finance's
public endpoints; it is not an official Yahoo product, and the data is subject to
Yahoo's terms. See the README for details.
