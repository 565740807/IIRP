# IIRP

**English** · [中文](README.zh-CN.md)

IIRP is a personal research tool for US stocks that runs on your own computer. It
collects SEC insider-trading filings and daily prices, and shows them so you can do
your own research:

- **Insider trades**: a live feed of Form 3/4/5 filings as SEC publishes them, and a
  lookup by ticker or name (last 6 months or last 10 trades by default), with the
  price change in the trading days before and after each trade.
- **Price studies**: monthly seasonality, any date range across years, and price
  reactions to earnings or to any event you name.

It does not rate trades, give signals or give investment advice. Data and settings
stay on your machine.

![Home page: market strip and the live insider feed](docs/images/home.png)

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) (Docker Engine with Compose v2
on Linux, Docker Desktop on macOS), Python 3 (only the standard library is used) and
git. On Windows, use WSL2 and keep the folder inside the Linux file system (for
example under `~`, not `/mnt/c`).

```bash
git clone https://github.com/565740807/IIRP.git iirp
cd iirp
./iirp start
```

The first run builds the image (a few minutes), creates `deploy/.env` from
`deploy/.env.example` (readable only by you, with a random database password), and
starts PostgreSQL, the web server and the background worker. Then open
<http://127.0.0.1:18081>.

**Add your SEC contact.** SEC asks automated tools to identify themselves with a name
and a real e-mail address. Until you add one, a notice at the top of the home page
says so and nothing is requested from SEC. Click **Add SEC contact**, enter your name
and e-mail, and insider filings start updating within a minute. It is stored only in
the local database and sent only to SEC. (You can instead set `IIRP_SEC_USER_AGENT`
in `deploy/.env`, for example `Jane Doe jane.doe@your-mail.com`; that value wins and
the page then shows it as set in the config file.)

What happens next: the worker follows new SEC filings (every minute or two on trading
days) and backfills the last 6 months of Form 3/4/5 filings, about 75,000 filings and
3 GB, which takes roughly a day at the polite rate IIRP uses (at most 2 requests per
second). Home-page index quotes refresh while the page is open. You can switch each
automatic update off under **Data & tasks → Automatic updates**.

Everyday commands:

```bash
./iirp status    # what is running
./iirp stop      # stop; data is kept in Docker volumes
./iirp start     # start again (also after git pull, to upgrade)
./iirp backup    # consistent backup of the database and saved filings
```

The page listens on `127.0.0.1:18081` only. To use another port, set
`IIRP_HTTP_PORT` in `deploy/.env`. The interface is in English; switch to Chinese at
the top right.

## Using IIRP

### Insider trades

![Company page: daily candles with insider buys and sells marked](docs/images/company.png)

- **Home**: new filings slide in at the top. Each card is one company on one filing
  day; each row is one insider: role, what they did (in SEC's wording), shares, value
  and trade date. Buys are blue with `+`, sales orange with `−`. If you scroll down,
  the list stays put and a "↑ N new trades" button appears instead.
- **Insiders**: search a ticker or a person's name. If IIRP has not seen it yet, it
  asks SEC and downloads only that company's or person's filings in range. The default
  range is the last 6 months; switch to the last 10 trades or a custom range.
- **Company and person pages** show daily candles with insider buys (▲) and sales (▼)
  marked, a trade table, and the price change n trading days before and after each
  trade (n = 3, 5, 10, 20 or your own; default 5). Each trade links to its detail page
  and to the original SEC filing.

### Analysis center

![Monthly analysis: year × month heat map, per-year candles and ranking](docs/images/monthly.png)

- **Monthly**: for up to 20 tickers over the last n years (default 8 plus this year),
  each month's return from the first trading day's open to the last trading day's
  close. You get a year × month heat map, one candle per year, and per month the
  median, the share of up years with a 95% interval, and how often it beat the
  benchmark (S&P 500 by default).
- **Interval**: the same for any date range, for example Dec 15 → Jan 10, including
  ranges that cross the new year.
- **Earnings** and **Events**: IIRP does not guess dates. It gives you a prompt, you
  ask any AI assistant, and you paste back a short JSON list:

  1. Enter tickers (and, for events, a description such as "Apple WWDC keynote"),
     then **Copy prompt**. The prompt templates can be edited and reset.
  2. Paste the prompt into the AI assistant of your choice and copy the JSON it
     returns.
  3. Paste the JSON into IIRP. It is checked as you paste; problems are listed by
     item and field. Click **Save and analyze**.

  ```json
  {"events": [
    {"ticker": "AAPL", "date": "2025-10-30", "session": "after_close",
     "name": "FY2025 Q4 earnings", "fiscal_year": 2025, "fiscal_quarter": 4},
    {"ticker": "AAPL", "date": "2025-06-09", "session": "during",
     "name": "WWDC 2025 keynote"}
  ]}
  ```

  `session` is `before_open`, `during`, `after_close` or `unknown`. A release after
  the close reacts on the next trading day, so results are centered on that reaction
  day: the n days before, the reaction day itself (and its opening gap), and the n
  days after, each with median, quartiles and the share of ups, plus one candle per
  event and the average path.

![Earnings analysis: reaction-day candles, average path and statistics](docs/images/earnings.png)

![Event analysis: Apple WWDC keynotes, AAPL and QQQ](docs/images/events.png)

Results can be exported as CSV, and charts as PNG. Daily prices are fetched once per
ticker for the whole range and kept for 24 hours; after that they are deleted with the
results computed from them, and reopening a study fetches them again.

## Data sources and terms

- **SEC EDGAR**: insider filings come from SEC's public EDGAR system. Read SEC's
  [guidance on accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data):
  automated access must identify itself, and IIRP stays far below SEC's rate limit.
- **Prices** come through [yfinance](https://github.com/ranaroussi/yfinance), an
  open-source library that reads Yahoo Finance's public endpoints. yfinance is **not
  an official Yahoo product** and is not affiliated with Yahoo; the data is subject to
  Yahoo's terms, is meant for personal research, and can be delayed, missing or
  corrected.
- IIRP gives no investment advice and no guarantee that any data is accurate,
  complete or timely.

## Security and privacy

- IIRP listens on `127.0.0.1` only and has **no login**. Do not expose it to the
  internet or your local network. To use it from another computer, use an SSH tunnel.
- Everything is stored locally in Docker volumes (`iirp2_postgres-data`,
  `iirp2_app-runtime`): filings, prices, your event lists, prompt templates, SEC
  contact and backups. The only things sent out are your SEC contact (to SEC, in each
  request) and ticker symbols (to Yahoo).
- `deploy/.env` holds the database password; it is ignored by git. Do not share it.

## How it works

```
browser ──► web (FastAPI) ──► PostgreSQL ◄── worker ──► SEC EDGAR / Yahoo (yfinance)
              reads only           ▲            fetches, parses, computes
                                   └── saved filings, backups (Docker volume)
```

The web server only reads the local database; every fetch is a durable task done by
the worker, so closing the browser does not stop work. All financial calculations are
in the Python backend; the React frontend only displays results. Details (in Chinese):
[architecture](docs/ARCHITECTURE.zh-CN.md), [installation and operations runbook](docs/RUNBOOK.zh-CN.md). The API is described in
[docs/openapi.json](docs/openapi.json).

## Development

```bash
./iirp check             # Ruff, backend tests, OpenAPI, frontend tests and build
./iirp test tests/sec    # some backend tests
./iirp frontend npm test # frontend tests
```

Tests and tools run in containers; backend tests use a throwaway PostgreSQL in tmpfs,
never the running instance's database. See [AGENTS.md](AGENTS.md) for conventions.

## License

[MIT](LICENSE). Third-party components keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
