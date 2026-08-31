# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A single-user FastAPI + MongoDB dashboard that turns Comdirect CSV exports into
categorised transactions and charts. Categorisation is done by the Claude API
(`app/classification/`), not by hand-written rules. The frontend is
server-rendered Jinja plus vanilla JS — there is no build step and no
node_modules.

## Commands

Everything goes through `mise` (`.mise.toml`), which is also what sets
`MONGODB_URL` and `DATABASE_NAME`. Running `uvicorn` or a script without those
two exported points the app at port 27017, where nothing is listening.

```bash
mise run dev      # dashboard on http://127.0.0.1:8000, starts MongoDB first
mise run test     # pytest — 400+ tests, under a second, no database needed
mise run lint     # ruff check + ruff format --check over app tests scripts
mise run format   # ruff format
mise run backup   # mongodump the working database before anything that rewrites it
```

`mise run <task>` for the maintenance scripts too (`migrate`, `backfill`,
`repair-refunds`, `eval`, `import-paypal`, `resolve-paypal`, `merchants`,
`migrate-merchant-directions`). Pass script arguments after `--`, e.g.
`mise run resolve-paypal -- --dry-run`. Every script that writes supports
`--dry-run`; run that first.

## The database

Two databases share the `mongo-bank` container on `localhost:27018`:

- **`bank_analyzer`** belongs to the previous version of this dashboard and is
  still in daily use. Never write to it, and avoid reading it.
- **`bank_analyzer_dev`** is this project's database. All work happens here.

The name must always come from `DATABASE_NAME` via `Settings` in
`app/config.py`. Never hard-code a database name in a script or helper — that
is the whole reason the setting exists. Take a `mise run backup` before running
anything that rewrites transactions.

## Architecture

```
CSV upload ─> app/parser.py ─> app/classification/service.py ─> MongoDB (Beanie)
                                                                     │
Jinja templates + fetch() <── app/routes.py <── app/services.py <─────┘
```

- `app/routes.py` — every HTTP handler. Page routes render templates; `/api/*`
  routes return JSON for the scripts in `app/static/`.
- `app/services.py` — all business logic behind the routes, as static-method
  service classes (`ChartDataService`, `ReviewService`, `RecurringService`,
  `TripService`, `CategorizationService`, `ComdirectBalanceService`).
- `app/models.py` — Beanie documents, registered in `app/database.py`. A new
  document model must be added to the `document_models` list there or it will
  silently not work.
- `app/categories.py` — **the** taxonomy. Nothing else may define a category
  list. `flow_type` (income/expense/transfer) is derived from the major
  category by `flow_type_for`, never guessed or stored independently.
  Transfers must stay out of every expense total and chart.
- `app/normalization.py` — booking line to stable merchant key. This is what
  makes the merchant cache work; changing it changes cache hit rates.
- `app/classification/` — the Claude API path: `prompt.py` (cached system
  block), `client.py` (batching, retries, usage), `service.py` (the funnel),
  `pricing.py` (model prices, dated).
- `app/paypal/` — recovers the payee Comdirect truncates out of PayPal lines by
  joining an activity export on amount, direction and settlement lag.
- `app/recurrence.py`, `app/trips.py`, `app/places.py`, `app/patterns.py` —
  detection heuristics for schedules, journeys, destinations and changes in
  the monthly spending. Pure functions over transactions, no database access;
  they are the easiest things here to test.
- `app/merchants.py` — the same shape, but a level rather than a change: who
  was paid over the last complete year, how few merchants hold the spending,
  and which categories are one merchant wearing a category's name. Nothing on
  the page it feeds is stored, because concentration is a property of the
  history rather than a proposal about it.

### Classification funnel

Order matters and is load-bearing: merchant cache (a `source="user"` entry
outranks the model) → PayPal payee recovery → deterministic fallback for
truncated PayPal lines with no payee → the API for what is left. Every API
result is then checked against the hierarchy and the confidence threshold;
anything failing either becomes `Uncategorized / Needs Review` rather than a
category nobody chose.

### Pattern detection

`app/patterns.py` decides which differences between one month and another mean
something. Three rules hold across every detector in it and are the reason it
produces a readable page rather than thirty cards:

- **Robust statistics, never means.** Levels are medians and spread is the
  median absolute deviation, so the holidays being looked for cannot raise the
  bar that is meant to catch them.
- **Every threshold is a share and a floor, and the floor is relative.** Every
  absolute test is a fraction of that ledger's own median month, so the same
  constants behave identically for a much larger or smaller one. A rule that
  fires above a fixed number of euros is a rule written for one dataset.
- **The trailing month is a lie until its statement lands.** `complete_months`
  drops it, and the first month too. Comparing against a four-day month
  reports the same spending collapse every time.

Findings are proposals, ranked by euros a month and nothing else, each
carrying the transactions that prove it. A `PatternDismissal` is keyed on the
finding's own facts — kind, subject, month — because findings are recomputed
from scratch on every page load.

### The legacy rule engine

`app/categorizer.py`, `app/constants.py`'s `CATEGORY_HIERARCHY`,
`app/legacy_mapping.py` and the `/categorize` and `/rules` pages are the
switched-off predecessor, kept so the decision is reversible
(`settings.rules_enabled`). Do not extend them, and do not delete them either.

## Conventions

**Detection proposes, the user decides.** Nothing the app infers — a schedule,
a trip, a category below threshold — may be created or named automatically. It
is offered as a blank proposal to confirm. Hand-made decisions live in their
own collections (`RecurringOverride`, `TripAssignment`, `DismissedTripProposal`)
rather than as fields on `Transaction`, because transactions get rewritten by
re-import, re-classification and the repair scripts and a user's decision must
not be in that blast radius. Keys are the facts that identify a thing
(merchant + period + amount, anchor transaction id), never a recomputed id.

**Rationale goes in the code, not on the page.** Module docstrings, `#:`
comments on constants and Jinja `{# #}` comments carry the reasoning — often at
length; that is the house style, match it. Rendered UI text stays minimal: data
and well-labelled controls, at most a short mono caption where a number would
otherwise look like a bug. No explanatory paragraphs in templates.

**Detail views are popups**, not panels pushed in between the charts.

**Other users exist.** Features and the taxonomy must not be narrowed to fit
one person's dataset.

**Data currency.** Statements are uploaded roughly monthly, so today's date is
never a proxy for how current the data is — read it from the transactions.

**Frontend.** Dark theme only. Chrome is monochrome; every saturated colour
belongs to data. Category colours are generated in `app/static/theme.js` from
the hierarchy, so a new category needs no frontend edit — never hand-list a
colour. Reference static files through the `static_url()` Jinja global, which
stamps the file mtime into the query string; a plain path gets served stale
from the browser cache.

**Python.** Ruff, line length 88, double quotes, target py311. Type hints on
signatures.

**Comments.** Don't write comments in the code, i wont read them, they are just a
waste of output tokens.

**Tests.** `tests/` mirrors the modules. They are pure unit tests —
`SimpleNamespace` stand-ins for documents, `monkeypatch` over the module-level
`Transaction`, `asyncio.run` for coroutines — and no MongoDB, so the suite runs
in under a second. Keep it that way. Test names are sentences describing the
behaviour, and the docstring says why the case matters.

**Commits.** One line, imperative, saying what changed for the user rather than
which files moved: "Recover the PayPal payee Comdirect truncates out of its
lines", not "Add paypal module".

## Secrets

`ANTHROPIC_API_KEY` lives in `.env`, which is gitignored. `.mise.toml` is in
version control and must never hold a key. Optional Comdirect API credentials
follow the same rule.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
