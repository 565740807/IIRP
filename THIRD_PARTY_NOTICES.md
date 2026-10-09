# Third-party notices

IIRP's own source code is released under the MIT License (see `LICENSE`). The license
covers this project's code only. IIRP does not bundle third-party code in this
repository: dependencies are downloaded from PyPI, npm and Docker Hub when you build
it, and each keeps its own license. Exact versions are locked in `uv.lock` and
`frontend/package-lock.json`.

## Direct dependencies

| Component | License |
|---|---|
| FastAPI, SQLAlchemy, Alembic, pydantic-settings, curl_cffi | MIT |
| Uvicorn, HTTPX | BSD-3-Clause |
| psycopg | LGPL-3.0-only |
| defusedxml | PSF License |
| yfinance, exchange_calendars | Apache-2.0 |
| React, React DOM, React Router, TanStack Query / Table / Virtual, i18next, react-i18next, Motion, NumberFlow, openapi-fetch, Radix UI, clsx, tailwind-merge, tw-animate-css | MIT |
| Apache ECharts, class-variance-authority | Apache-2.0 |
| Lucide icons | ISC |
| Geist font (@fontsource-variable/geist) | SIL Open Font License 1.1 |
| UI components adapted from [shadcn/ui](https://github.com/shadcn-ui/ui) | MIT |

Transitive dependencies (for example pandas and NumPy, pulled in by yfinance) keep
their own licenses. To list everything that is installed, you can use tools such as
[pip-licenses](https://github.com/raimon49/pip-licenses) for Python and
[license-checker](https://github.com/davglass/license-checker) for npm.

## Container images

The images are pinned by digest in `deploy/Dockerfile` and `deploy/compose.yaml`:
the official Python, Node.js and PostgreSQL images. PostgreSQL is distributed under
the [PostgreSQL License](https://www.postgresql.org/about/licence/). Debian and PGDG
packages installed while building the image are not snapshot-pinned.

## Data sources

SEC EDGAR data is public. Prices come through yfinance, which reads Yahoo Finance's
public endpoints; it is not an official Yahoo product, and the data is subject to
Yahoo's terms. See the README for details.
