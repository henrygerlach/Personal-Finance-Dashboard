# 🏦 Comdirect Personal Finance Dashboard
**A Fullstack FastAPI & MongoDB Application for Automated Wealth Tracking.**

[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)](https://fastapi.tiangolo.com)
[![MongoDB](https://img.shields.io/badge/MongoDB-%234ea94b.svg?style=flat&logo=mongodb)](https://www.mongodb.com)
[![Poetry](https://img.shields.io/badge/Poetry-%233B48CC.svg?style=flat&logo=python)](https://python-poetry.org)
[![Tested with Pytest](https://img.shields.io/badge/Tests-Passing-brightgreen?logo=pytest)](/tests)

> **The Value:** Transforming messy CSV bank exports into actionable financial insights. Featuring a custom rule-based engine for automated transaction categorization.

## 🖼️ Dashboard Preview
![Dashboard Overview](assets/dashboard.png)
*Interactive charts showing spending patterns and net cashflow trends.*

## 🧩 Create Rule Preview
![Create Rule View](assets/categorize.png)
*Rule creation interface for assigning automatic transaction categories. Rules are composed of a substring of the booking text and an optional process.*

![Creating a rule](assets/create-rule.gif)
*Filing one transaction: pick the categories, enter the substring, optionally pin the process, and save. The queue advances and the new rule lands in the rule library.*

## 🚀 Technical Highlights & "The Hard Stuff"
* **Advanced Categorization Engine:** Implemented a rule-based matching system that evaluates rules in creation order to automate 90% of manual bookkeeping.
* **Data Integrity:** MD5-based hashing for transaction deduplication – ensuring clean data even with overlapping CSV imports.
* **Modern Stack:** Asynchronous database access via **Beanie ODM**, containerized environment with **Docker**, and dependency management with **Mise/Poetry**.
* **Optional Startup Sync:** On app start, account balances can optionally be fetched from the Comdirect API to refresh balance snapshots.
* **Resilience:** Implemented a fallback mechanism for Comdirect API outages, serving cached balance snapshots from MongoDB.

## 🏗️ System Architecture
```mermaid
graph LR
    subgraph Data_Sources
        CSV[Comdirect CSV]
        API[Comdirect API]
    end

    subgraph Backend_App ["FastAPI Core"]
        Parser[CSV Parser]
        Engine[Categorizer Engine]
        Serv[Business Services]
    end

    subgraph Storage
        DB[(MongoDB / Beanie)]
    end

    subgraph Frontend
        UI[Chart.js Dashboards]
    end

    CSV --> Parser
    API --> Serv
    Parser --> Engine
    Engine --> DB
    Serv --> UI
    DB --> Serv
```

## 🚀 Quick Start
This project uses mise for toolchain management, environment and tasks.

```Bash
# 1. Toolchain and dependencies
mise trust          # mise refuses to read an untrusted config
mise install
mise run install

# 2. The API key for classification
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# 3. Launch — the database starts with it
mise run dev
```

Visit http://127.0.0.1:8000.

`.mise.toml` sets `MONGODB_URL` and `DATABASE_NAME`. Without it the app falls
back to port 27017 while the container publishes 27018, and dies on the first
query — so start through mise, or export those two yourself.

The API key stays out of `.mise.toml` on purpose: that file is in version
control, `.env` is not.

### Tasks

| | |
|---|---|
| `mise run dev` | the dashboard, with auto-reload |
| `mise run mongo` | just the database |
| `mise run test` / `lint` / `format` | the usual |
| `mise run backup` | dump the working database before anything that rewrites it |
| `mise run merchants` | how well normalization collapses transactions onto merchants |
| `mise run migrate` | migrate categories to the new taxonomy (`-- --dry-run` to preview) |
| `mise run backfill` | classify transactions still holding a provisional category |
| `mise run repair-refunds` | re-file transactions whose category disagrees with their direction |
| `mise run eval` | score the classifier against the hand-made categories |

## 🍏 Start & Stop from Spotlight (macOS)
Install two small app bundles once:

```Bash
./scripts/install-macos-app.sh
```

Afterwards `cmd+space` → **Comdirect Dashboard** starts MongoDB and the server and opens
the browser once the page really answers; **Stop Comdirect Dashboard** shuts both down
again — the container is only stopped, never removed, so all transactions and rules stay.

The bundles only contain a stub; the logic lives in `scripts/launcher.sh` and
`scripts/stopper.sh`, so changes take effect without reinstalling. Both write to
`~/Library/Logs/Comdirect-Dashboard.log`.

> The project lives in the Documents folder, which macOS protects, so both bundles
> need permission for it — without it they cannot even read their own scripts. Their
> stub is therefore a small compiled binary (built by the install script, which needs
> the Xcode command line tools): macOS decides about folder access by the signature of
> the running process, and a shell script as the bundle's executable would run as
> `/bin/bash` — a system binary the granted permission never matches, so the app would
> be denied silently and do nothing at all.
>
> Should an app still report missing access, add it under *System Settings → Privacy &
> Security → Full Disk Access*, or let macOS ask again:
>
> ```Bash
> tccutil reset SystemPolicyDocumentsFolder local.comdirect.dashboard
> tccutil reset SystemPolicyDocumentsFolder local.comdirect.stopper
> ```
