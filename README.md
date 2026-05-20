# AMC+

An automation tool that monitors AMC Theatres for movie showtimes and seat  
availability across multiple movies and theatres in parallel, scores every open seat  
with a custom scoring function, and notifies via Discord Webhook when  
qualifying seats appear — with an optional auto-reserve path that places held orders  
through AMC's GraphQL API.

![Python](https://img.shields.io/badge/Python-3.13+-blue)

---

## Overview

AMC+ runs and monitors AMC Theatres for movie showtimes and seat availability. For each configured "profile" (one movie + one or more theatres + a filter set), it does the following on a loop:

1. **Checks** partial movie names and theatre names against AMC's REST and GraphQL
  APIs in parallel.
2. **Discovers** showtimes across all configured theatres for a rolling date window,
  filtered by format (IMAX 70MM, Dolby, Laser, etc.), seating type
   (recliners / rockers), subtitle preferences, and AMC's Tuesday/Wednesday 50%-off
   eligibility.
3. **Fetches each showtime's seat layout** via GraphQL, scores every available seat,
  finds adjacent groups of the requested size, and detects connected loveseat
   recliner pairs.
4. **Compares against previous-scan state** to detect new seat groups
  (usually from people cancelling) and brand-new showtimes.
5. **Notifies** via Discord webhook, and optionally **reserves** the seats. The user finishes checkout in a browser, logging in if they want, before the ~8-minute hold expires.

## Why I Built This

Project Hail Mary, IMAX 70MM. That is the entire reason. I was very late to the Project Hail Mary wave, and by the time I hopped on, every single seat, at every single showtime that was available was booked. ONLY very few seats in the very first row in front of the screen were available, and I didn't want to strain my neck, and REALLY wanted an optimal viewing experience for my very first time viewing IMAX 70MM - I wanted as close to what Christopher Nolan said during an interview as the most "ideal" seats in the theater for viewing experience. With there being ONE theater in New York City with true IMAX 70MM (AMC Lincoln Square), and the movie having so much hype, it was very difficult to get seats. I figured that leading right up to showtime, people who couldn't make it for whatever reason would cancel their tickets, and request a refund right before their window of receiving a refund for their tickets would pass, and those seats would open up. With the theater being 15 minutes away via public transportation from my campus in the Upper East Side, I was able to get 2 tickets to Project Hail Mary about 40 minutes before showtime, seats F24 & F25, scored 88.04 and 86.04 respectively, and I was able to catch the Dune Part Three trailer in IMAX 70MM, along with the extended trailer for The Odyssey, also in IMAX70mm.

## Features

- **Dual-source API client** with parallel reads. A single `AMCClient` fires  
requests against both REST API and GraphQL using a `ThreadPoolExecutor`. The first valid response wins; discrepancies
are logged.
- **TLS fingerprint impersonation** via `curl_cffi` to bypass Cloudflare's JA3  
fingerprinting.
- **Multi-profile concurrency.** Each movie is its own profile with its own filter  
set, thread pool, discovery thread, and held-orders list. Per-profile overrides global defaults.
- **Custom seat-scoring algorithm.** Each open seat is scored 0–100 using a geometric  
model — target row at 62% back, dead center, with weighted row/column distance  
penalties for front rows, back rows, edge columns, and accessibility seats, plus a  
small bonus for connected loveseat pairs. Based on the score, seats are grouped into quality tiers  
(ELITE / GREAT / GOOD / OK).
- **Loveseat-aware group finding.** When the configured `quantity` is even and the
profile filters to recliner seating, the group finder prefers physically connected
`LoveSeatLeft + LoveSeatRight` pairs over any random adjacent seats, to prevent the awkward situation of you and the person you're going with being oddly seperated whilst also sharing your loveseat with a stranger (which is no big deal at all, but ideally you'd want to be with the people/person you came with)
- **Two monitoring modes:**
  - **Snipe** — Scans for new seat(s) being available on a showtime, alerts and reserves when  
  new groups appear (cancellations or holds released).
  - **Release** — speculative batch fire on ticket-drop day across a showtime, purely for securing tickets for showtimes on the day the movie releases (release day), mainly just for the Avengers movie later this year - knowing how spoilers get on social media. Automatically transitions  
  back to snipe after firing.
- **Background discovery engine.** A per-profile thread re-fetches showtimes on a  
configurable interval, checks against the known showtime-ID set, and triggers  
aggressive acquisition for newly seen shows — catching AMC's silent additions of  
new IMAX 70mm screenings - basically a release mode, as they are brand new showtimes.
- **Phase-based load shedding.** Profiles automatically downshift to `dormant` mode
for movies more than ~56 days from release (metadata-only refresh every 4 hours),
shift to `watch` within 8 weeks, and to full `active` scanning within 14 days. An
optional `early_drop_check` probes the release-day window even while dormant to  
catch silent presale drops - I made this last early_drop_check when Dune: Part Three released release day tickets months ahead of time out of nowhere (probably to solidify they aren't giving up their IMAX screens to Disney for Avengers Doomsday, but then again, The Odyssey sold release day tickets in 2025 on the day that is exactly one year before the movie releases)
- **Waiting mode** for movies that aren't on AMC's catalog yet. The monitor
re-attempts resolution every ~5 minutes and promotes the profile to live tracking
once the movie appears.
- **Resilience layer:**
  - TTL cache (300 s) for resolution lookups
  - Per-endpoint circuit breaker after 20 consecutive failures
  - Exponential backoff with jitter
  - `Retry-After`-aware handling of HTTP 429
  - Distinct handling of 4xx vs 5xx vs network errors
- **Discord notifications** with rich embeds for startup, snipe hits, new showtimes,  
release-mode fires, phase transitions, mode transitions, seating fallback, movie  
found, waiting list, and shutdown. Notifications go through a serialized post  
queue with a 650ms minimum gap and a Retry-After-honoring retry loop, so the  
bot won't trip Discord's rate limits even during a burst.
- **Atomic state persistence.** Writes to `state/amc_state.json` go through a
temp-file → `fsync` → atomic-rename pipeline so a crash mid-write can't corrupt
the file. On restart, the audit step logs orphan profile keys and restored held
orders.
- **Graceful shutdown.** `SIGINT` / `SIGTERM` saves state, sends a Discord shutdown
embed with session stats, and exits cleanly.
- **CLI toolkit (`amc_toolkit.py`)** for ad-hoc operations: movie lookup, theatre
lookup, seat-map render (terminal grid with emoji tier colors), discover, reserve,
cancel, health check.
- **Standalone release watcher (`amc_watcher.py`)** that polls AMC's
`COMING_SOON` / `NOW_PLAYING` catalogs and alerts on status transitions
(`ADVANCE_TICKETS`, `NOW_PLAYING`) — complementary to the main monitor.

## Tech Stack

- **Language:** Python 3.10+
- **HTTP:** `requests` + `curl_cffi`
- **Concurrency:** `concurrent.futures.ThreadPoolExecutor`, `threading.Event`,
`threading.Lock`
- **Persistence:** atomic JSON file write (`os.fsync` + `os.replace`)
- **Timezones:** `zoneinfo.ZoneInfo`
- **CLI:** `argparse`
- **Config:** JSONC
- **Runtime:** single Python process; optionally containerised via the included
`Dockerfile`

## Architecture

```
amc_monitor.py
  Config + ProfileRuntime          ← config + per-profile state
  ProfileRunner                 ← snipe scan loop, state diff, reserve
  DiscoveryEngine               ← per-profile background thread
  Discord embed + send layer    ← serialised, rate-limit-aware
        │
        ▼
amc_api.AMCClient
  ├── OfficialAMCClient         REST
  │   
  └── GraphQLClient             GraphQL
```

## How It Works

On startup:

1. Load `config.jsonc`
2. Check the REST API key against a known theatre.
3. Resolve each profile's movie and theatres in parallel; profiles that can't resolve
  enter Waiting mode (assumed that the movie hasn't been listed on AMC Theaters website yet, they may be far out of whatever the case may be) and are retried in the background.
4. For each resolved profile, run initial discovery, build a `ProfileRunner`, and
  start its discovery thread.
5. If any profile's mode is `release`, fire `release_mode` immediately against the
  initial showtime set, then transition to `snipe`.
6. Send a paginated startup embed to Discord per movie (this is a bit spammy I should change this)

The main loop then runs one snipe pass per profile per cycle. Each pass compares the  
current scan's seat-group IDs against the previous scan's IDs (persisted in the state  
file). New groups are alerted via Discord Webhook and, if `auto_reserve` is on, a GraphQL call. Per-group alert cooldown and stale-hold pruning prevent spam.

The background discovery engine per profile runs independently. When it sees a new  
showtime ID for the first time (AMC adds a midnight IMAX screening), it calls  
the aggressive acquisition path: best seats in the house, nothing else.

## Installation

Requires Python 3.10+.

```bash
pip install -r requirements.txt
```

### Docker

```bash
docker build -t amc-plus .
docker run \
  -v "$(pwd)/state:/app/state" \
  -v "$(pwd)/config.jsonc:/app/config.jsonc" \
  amc-plus
```

## Configuration

Configuration lives in `config.jsonc`. Only **one** field is truly global:

```jsonc
"discord": {
  "webhook_url": "https://discord.com/api/webhooks/..."
}
```

Everything else is a default that any profile in the `profiles[]` array can override.
A minimal profile:

```jsonc
"profiles": [
  {
    "movie": "the-odyssey",
    "theatre": "amc-lincoln-square-13",
    "format": "IMAX 70MM",
    "mode": "release",
    "quantity": 2,
    "tier": "ELITE",
    "auto_reserve": true,
    "max_orders": 3
  }
]
```

## Usage

```bash
# Main monitor (foreground)
python3 amc_monitor.py

# One-shot dashboard
python3 amc_monitor.py --status

# Single scan and exit
python3 amc_monitor.py --oneshot

# Verbose logging
python3 amc_monitor.py --log-level DEBUG

# Clear persisted state
python3 amc_monitor.py --reset-state

# Toolkit
python3 amc_toolkit.py movie "project hail mary"
python3 amc_toolkit.py theatre lincoln
python3 amc_toolkit.py seats 123456
python3 amc_toolkit.py health

# Catalog-status watcher
python3 amc_watcher.py -m the-odyssey --webhook "<url>"
```

