# Personal Finance Dashboard

**Turns a Comdirect bank CSV export into a categorised ledger, and surfaces the recurring payments, trips and month-over-month changes hidden inside it.**

> This project is AI-coded: Claude (via Claude Code) wrote essentially all of the code. Henry Gerlach's role was the idea, the CLAUDE.md conventions it had to follow, review of every change, and the taxonomy/product decisions.

[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)](https://fastapi.tiangolo.com)
[![MongoDB](https://img.shields.io/badge/MongoDB-%234ea94b.svg?style=flat&logo=mongodb)](https://www.mongodb.com)
[![Poetry](https://img.shields.io/badge/Poetry-%233B48CC.svg?style=flat&logo=python)](https://python-poetry.org)
[![CI](https://github.com/henrygerlach/Personal-Finance-Dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/henrygerlach/Personal-Finance-Dashboard/actions/workflows/ci.yml)

![Dashboard](assets/dashboard.png)

There's no hosted demo, because the input is a bank statement. Instead:

```bash
mise run seed-demo   # generates two years of a synthetic current account
mise run dev-demo    # http://127.0.0.1:8000
```

Every screenshot here is of that generated ledger — no real statement appears in this repo.

## Why I built it

A German bank export is just a booking line, an amount and a date. The merchant is buried in machine-generated text that changes on every visit to the same shop, and payment processors like PayPal overwrite it entirely — so "what did I spend on groceries in March" isn't a question the statement can answer on its own.

The previous version of this dashboard answered that by hand: 609 substring-matching rules over 2,676 transactions, and every new merchant meant a 610th rule. This version deletes the rule library and hands the booking line to an LLM instead, but puts the API call last — behind a merchant cache and two deterministic recovery steps — so most transactions never reach it.

## Features

**Import**
- Comdirect CSV upload, MD5-deduplicated so re-uploading an overlapping export is harmless.
- PayPal activity exports, joined against bank lines by amount, direction and settlement lag to recover the payee Comdirect truncates out.

**Classification**
- Every transaction gets one of 73 minor categories under 14 majors, via the Claude API, validated against the taxonomy and a confidence threshold before it's stored.
- A merchant cache answers first so a given merchant is only ever classified once, and a `source="user"` entry (filed by hand) permanently outranks the model.
- Anything the classifier declines to guess — rather than guessing badly — lands in a review queue for a human to file.

![Filing a transaction from the review queue](assets/review.gif)

**Detection** (all proposals, none auto-created — see Conventions below)
- Recurring payments and their price rises over time.
- Trips: stretches of card activity away from home, and what each one actually cost.
- Month-over-month changes worth noticing, using robust statistics (medians, MAD) so a single expensive month doesn't skew the baseline that's meant to catch it.
- Merchant concentration: which merchants a category total is actually made of.

![Trips](assets/trips.png)

**Everywhere**
- Every number on every chart drills down into the transactions behind it, as a popup rather than a panel shoved between charts.

## Architecture

```
CSV upload ──▶ parser.py (dedup) ──▶ normalization.py (booking line → merchant key)
                                              │
PayPal export ──▶ paypal/ (payee recovery) ──▶│
                                              ▼
                          ┌─── classification funnel ───┐
                          │ merchant cache               │
                          │   → PayPal recovery           │
                          │     → deterministic fallback   │
                          │       → Claude API (last resort)│
                          └──────────────┬───────────────┘
                                         ▼
                                  MongoDB (Beanie)
                                         │
                                         ▼
                  detection: recurrence · trips · patterns · merchants
                                         │
                                         ▼
                       app/services/ ──▶ routes.py ──▶ Jinja + vanilla JS
```

- **Classification funnel** — ordered so the API is the last, not the first, thing tried. Order is load-bearing: the merchant cache wins over everything, including a fresh API answer, because a classifier is not deterministic and the ledger shouldn't move under the charts between page loads.
- **Detection modules** (`app/recurrence.py`, `app/trips.py`, `app/patterns.py`, `app/merchants.py`) are pure functions over transaction lists, with no database access of their own. That's a deliberate constraint — it's what keeps the test suite fast and the logic easy to reason about.
- **`app/services/`** holds the business logic behind every route as static-method classes, one per page. Routes stay thin: render a template or return JSON.
- **The legacy rule engine** (`app/categorizer.py`, `CATEGORY_HIERARCHY`, the `/rules` page) still exists, switched off behind a flag, rather than deleted — so replacing it stays a reversible decision, not a one-way door.
- **Frontend** is server-rendered Jinja plus vanilla JS. No build step, no `node_modules`, no framework. For a single-user dashboard that's less to maintain, not a limitation.

## Setup

Prerequisites: [mise](https://mise.jdx.dev), Docker, and an Anthropic API key.

```bash
git clone https://github.com/henrygerlach/Personal-Finance-Dashboard.git
cd Personal-Finance-Dashboard

mise trust          # mise refuses to read an untrusted config otherwise
mise install        # installs Python 3.11 + Poetry
mise run install    # poetry install --all-extras

cp .env.example .env   # then put your Anthropic key in it

mise run dev        # starts MongoDB in Docker, serves http://127.0.0.1:8000
```

Then either upload a Comdirect CSV at `/upload`, or skip straight to the synthetic ledger:

```bash
mise run seed-demo
mise run dev-demo
```

> `.mise.toml` sets `MONGODB_URL` (port 27018, not the default 27017) and `DATABASE_NAME`. Running `uvicorn` directly without going through mise points the app at 27017, where nothing is listening.

<details>
<summary>All mise tasks</summary>

| Task | What it does |
|---|---|
| `dev` / `mongo` | dashboard with auto-reload / just the database |
| `test` / `coverage` | pytest / pytest with coverage report |
| `lint` / `format` | ruff check + format --check / ruff format |
| `backup` | dump the working database before anything that rewrites it |
| `seed-demo` / `dev-demo` | generate and serve the synthetic ledger |
| `merchants` | report how well normalization collapses transactions onto merchants |
| `migrate` | migrate categories to the current taxonomy |
| `backfill` | classify transactions still holding a provisional category |
| `repair-refunds` | re-file transactions whose category disagrees with their direction |
| `import-paypal` / `resolve-paypal` | import PayPal activity exports, then re-file transactions with the recovered payee |
| `eval` | score the classifier against hand-made categories |

Every task that writes to the database supports `--dry-run` (e.g. `mise run resolve-paypal -- --dry-run`). Run that first.

</details>

<details>
<summary>Start and stop from Spotlight (macOS)</summary>

```bash
./scripts/install-macos-app.sh
```

Installs app bundles so `cmd+space` → **Personal Finance Dashboard** starts MongoDB and the server and opens a browser tab; **Stop Personal Finance Dashboard** shuts both down.

</details>

## Testing and quality

`mise run test` runs 857 cases in about a second, with no database — the detection modules are pure functions, and everything else is stood in for with `SimpleNamespace` and `monkeypatch`. `mise run coverage` reports 97% line coverage. GitHub Actions runs lint, format and the test suite on every push; there's no database service container in CI, so a run can't run up an API bill or need secrets.

## Status

Single-user, self-hosted, no authentication — meant to run on localhost only. In weekly use against my own Comdirect statements, which arrive roughly monthly. Development is occasional, not finished; the rule engine stays switched off behind a flag rather than deleted until there's no reason left to keep it.

## License

MIT. See [LICENSE](LICENSE).
