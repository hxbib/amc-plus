from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import requests
from amc_api import AMCClient, BASE

log = logging.getLogger("amc.watcher")
VERSION = "5.23.25"
PRODUCT = "AMC+"
DISCORD_USERNAME = "AMC+"
DISCORD_AVATAR = "https://i.imgur.com/Pll0t9Y.jpeg"
FOOTER_ICON = "https://i.imgur.com/Pll0t9Y.jpeg"
AUTHOR_ICON = "https://i.imgur.com/Pll0t9Y.jpeg"
AUTHOR_URL = "https://www.amctheatres.com/"
TMDB_API_KEY = "getyourowntmdbapikey"
TMDB_READ_ACCESS_TOKEN = (
    "getyourownaccesskey.nope"
)
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"
AMC_MOVIE_URL = "https://www.amctheatres.com/movies"


def _footer_stamp(tz: str) -> str:
    try:
        dt = datetime.now(tz=ZoneInfo(tz))
    except Exception:
        dt = datetime.now().astimezone()
    return dt.strftime("%m-%d-%Y %I:%M:%S %p %Z")


def _fmt_date(iso: str) -> str:
    try:
        y, m, d = iso[:10].split("-")
        return f"{m}-{d}-{y}"
    except Exception:
        return iso


def _get_tmdb_poster(name: str) -> str:
    if not name:
        return ""
    try:
        r = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"query": name, "page": 1},
            headers={"Authorization": f"Bearer {TMDB_READ_ACCESS_TOKEN}"},
            timeout=6,
        )
        if r.status_code == 401:
            r = requests.get(
                "https://api.themoviedb.org/3/search/movie",
                params={"api_key": TMDB_API_KEY, "query": name, "page": 1},
                timeout=6,
            )
        if r.ok:
            results = r.json().get("results", [])
            if results and results[0].get("poster_path"):
                return f"{TMDB_IMG_BASE}{results[0]['poster_path']}"
    except Exception:
        pass
    return ""


def _format_runtime_minutes(mins: int) -> str:
    if not mins or mins < 1:
        return ""
    h, m = divmod(int(mins), 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _watcher_footer(tz: str) -> dict:
    return {"text": f"AMC+ v{VERSION} · by habib · {_footer_stamp(tz)}", "icon_url": FOOTER_ICON}


def _watcher_author() -> dict:
    return {"name": PRODUCT, "icon_url": AUTHOR_ICON, "url": AUTHOR_URL}


class MovieState:
    def __init__(self, slug: str, name: str = "", movie_id: int = 0):
        self.slug = slug
        self.name = name or slug.rsplit("-", 1)[0].replace("-", " ").title()
        self.movie_id = movie_id
        self.run_time_minutes: int = 0
        self.status: Optional[str] = None
        self.prev_status: Optional[str] = None
        self.release_date: Optional[str] = None
        self.ticket_avail_date: Optional[str] = None
        self.genre: Optional[str] = None
        self.mpaa_rating: Optional[str] = None
        self.has_scheduled_showtimes: bool = False
        self.showtime_count: int = 0
        self.last_check: Optional[str] = None
        self.alerted_avail: bool = False
        self.alerted_advance: bool = False
        self.alerted_playing: bool = False
        self.source: str = ""

    def to_dict(self) -> Dict:
        return {k: getattr(self, k) for k in vars(self)}

    @staticmethod
    def from_dict(d: Dict) -> "MovieState":
        ms = MovieState(d["slug"], d.get("name", ""), d.get("movie_id", 0))
        for k, v in d.items():
            if hasattr(ms, k):
                setattr(ms, k, v)
        return ms


class ReleaseWatcher:
    def __init__(
        self,
        watch_slugs: Optional[List[str]] = None,
        watch_all: bool = False,
        theatre_slug: str = "amc-lincoln-square-13",
        theatre_name: str = "",
        tz: str = "America/New_York",
        poll_interval: int = 120,
        state_path: str = "./state/watcher_state.json",
        discord_webhook: str = "",
        discord_avatar: str = "https://cdn.brandfetch.io/idr0UBBrXV/w/400/h/400/theme/dark/icon.jpeg",
        ignore_genres: Optional[List[str]] = None,
    ):
        self.watch_slugs: Optional[Set[str]] = set(watch_slugs) if watch_slugs else None
        self.watch_all = watch_all
        self.theatre_slug = theatre_slug
        self.theatre_name = theatre_name
        self.tz = tz
        self.poll_interval = poll_interval
        self.state_path = state_path
        self.discord_webhook = discord_webhook
        self.discord_avatar = discord_avatar
        self.ignore_genres: Set[str] = set(ignore_genres or [])
        self.client = AMCClient()
        self.movies: Dict[str, MovieState] = {}
        self._stop = False

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.tz))

    def _load(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    for s, d in json.load(f).get("movies", {}).items():
                        self.movies[s] = MovieState.from_dict(d)
                log.info("Loaded %d movies from state", len(self.movies))
            except Exception as e:
                log.warning("State load failed: %s", e)

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        try:
            with open(self.state_path, "w") as f:
                json.dump(
                    {"movies": {s: m.to_dict() for s, m in self.movies.items()}},
                    f,
                    indent=2,
                    default=str,
                )
        except Exception as e:
            log.error("State save failed: %s", e)

    def _fetch_movies(self) -> List[Dict]:
        all_m: List[Dict] = []
        sources_ok: List[str] = []

        try:
            coming = self.client.get_coming_soon(200)
            all_m.extend(coming)
            sources_ok.append(f"coming_soon={len(coming)}")
        except Exception as e:
            log.debug("GQL coming soon failed: %s", e)

        try:
            playing = self.client.get_now_playing(200)
            all_m.extend(playing)
            sources_ok.append(f"now_playing={len(playing)}")
        except Exception as e:
            log.debug("GQL now playing failed: %s", e)

        log.debug("Catalog fetch: %s total=%d", ", ".join(sources_ok), len(all_m))

        found_slugs = {m.get("slug") for m in all_m if m.get("slug")}
        if self.watch_slugs:
            for slug in self.watch_slugs:
                if slug not in found_slugs:
                    try:
                        resolved = self.client.resolve_movie(slug)
                        if resolved:
                            mid = resolved.get("id", 0)
                            if mid:
                                full = self.client.get_movie_by_id(mid)
                                if full:
                                    all_m.append(full)
                                    log.debug("REST direct lookup: %s → id=%d", slug, mid)
                                    continue
                            all_m.append({
                                "slug": resolved.get("slug", slug),
                                "name": resolved.get("name", ""),
                                "movieId": resolved.get("id", 0),
                                "status": resolved.get("status", ""),
                                "releaseDateUtc": resolved.get("release_date", ""),
                                "onlineTicketAvailabilityDateUtc": resolved.get("ticket_avail_date", ""),
                                "mpaaRating": resolved.get("mpaa_rating", ""),
                                "genre": resolved.get("genre", ""),
                                "runTime": resolved.get("run_time", 0) or 0,
                            })
                            log.debug("REST resolve fallback: %s", slug)
                    except Exception as e:
                        log.debug("REST fallback for %s failed: %s", slug, e)

        return all_m

    def check(self) -> List[Dict]:
        now_str = self._now().isoformat()
        changes: List[Dict] = []

        try:
            movies = self._fetch_movies()
        except Exception as e:
            log.error("Movie fetch failed: %s\n%s", e, traceback.format_exc())
            return changes

        for movie in movies:
            slug = movie.get("slug", "")
            if not slug:
                continue
            if self.watch_slugs and slug not in self.watch_slugs:
                continue
            if self.watch_all and (movie.get("genre") or "") in self.ignore_genres:
                continue

            if slug not in self.movies:
                self.movies[slug] = MovieState(slug, movie.get("name", ""), movie.get("movieId", 0))
            ms = self.movies[slug]
            ms.last_check = now_str

            field_map = {
                "name": "name",
                "genre": "genre",
                "mpaa_rating": "mpaaRating",
                "release_date": "releaseDateUtc",
            }
            for attr, key in field_map.items():
                v = movie.get(key)
                if v:
                    setattr(ms, attr, v)

            if movie.get("runTime") is not None:
                try:
                    ms.run_time_minutes = int(movie["runTime"])
                except (TypeError, ValueError):
                    pass

            if movie.get("hasScheduledShowtimes") is not None:
                ms.has_scheduled_showtimes = movie["hasScheduledShowtimes"]

            new_s = movie.get("status")
            new_a = movie.get("onlineTicketAvailabilityDateUtc")

            if new_a and new_a != ms.ticket_avail_date:
                old = ms.ticket_avail_date
                ms.ticket_avail_date = new_a
                if (old or ms.last_check) and not ms.alerted_avail:
                    changes.append({"type": "ticket_date_set", "movie": ms, "date": new_a})
                    ms.alerted_avail = True

            if new_s and new_s != ms.status:
                old_s = ms.status
                ms.prev_status = old_s
                ms.status = new_s
                if old_s is None:
                    if new_s == "ADVANCE_TICKETS":
                        ms.alerted_advance = True
                    elif new_s == "NOW_PLAYING":
                        ms.alerted_playing = True
                elif new_s == "ADVANCE_TICKETS" and not ms.alerted_advance:
                    changes.append({"type": "tickets_live", "movie": ms, "old": old_s})
                    ms.alerted_advance = True
                elif new_s == "NOW_PLAYING" and not ms.alerted_playing:
                    changes.append({"type": "now_playing", "movie": ms})
                    ms.alerted_playing = True
                else:
                    changes.append({"type": "status_change", "movie": ms, "old": old_s, "new": new_s})
            elif new_s:
                ms.status = new_s

        return changes

    def _build_embed(self, title: str, color: int, ms: "MovieState", description: str = "") -> Dict:
        poster = _get_tmdb_poster(ms.name)
        movie_url = f"{AMC_MOVIE_URL}/{ms.slug}" if ms.slug else AMC_MOVIE_URL
        embed: Dict = {
            "title": title,
            "url": movie_url,
            "color": color,
            "author": _watcher_author(),
            "footer": _watcher_footer(self.tz),
        }
        if description:
            embed["description"] = description
        if poster:
            embed["thumbnail"] = {"url": poster}

        fields: List[Dict] = []
        if ms.release_date:
            fields.append({"name": "Release Date", "value": f"`{_fmt_date(ms.release_date)}`", "inline": True})
        rt = _format_runtime_minutes(ms.run_time_minutes)
        if rt:
            fields.append({"name": "Runtime", "value": f"`{rt}`", "inline": True})
        if ms.mpaa_rating:
            fields.append({"name": "Rating", "value": f"`{ms.mpaa_rating}`", "inline": True})
        if ms.genre:
            fields.append({"name": "Genre", "value": f"`{ms.genre}`", "inline": True})
        if ms.status:
            fields.append({"name": "Status", "value": f"`{ms.status}`", "inline": True})
        if fields:
            embed["fields"] = fields
        return embed

    def _notify(self, changes: List[Dict]):
        if not self.discord_webhook or not changes:
            return
        embeds = []
        for c in changes:
            ms = c["movie"]
            if c["type"] == "tickets_live":
                desc = f"🎟️ **{ms.name}** tickets are **NOW ON SALE!**\n\n"
                desc += f"`{c.get('old', '?')}` → **`ADVANCE_TICKETS`**\n"
                if ms.release_date:
                    desc += f"📅 Release: **`{_fmt_date(ms.release_date)}`**\n"
                if ms.ticket_avail_date:
                    desc += f"🛒 On-Sale Since: `{_fmt_date(ms.ticket_avail_date)}`\n"
                desc += "\n**Go secure your seats now!**\n\n"
                desc += "_This watcher only notifies on catalog status._ **AMC+ Monitor** (`amc_monitor.py`) is what scans showtimes and can **auto-reserve** seats when your profiles match — keep both running for the full pipeline."
                embeds.append(self._build_embed(f"🚨  TICKETS LIVE — {ms.name}", 0xFF0000, ms, desc))

            elif c["type"] == "ticket_date_set":
                date_str = _fmt_date(c["date"])
                desc = f"AMC has set an on-sale date for **{ms.name}**.\n\n"
                desc += f"🛒 **Tickets Available:** `{date_str}`\n"
                if ms.release_date:
                    desc += f"📅 **Release:** `{_fmt_date(ms.release_date)}`\n"
                desc += "\nMark your calendar — tickets drop on this date."
                embeds.append(self._build_embed(f"📅  On-Sale Date — {ms.name}", 0xFFA500, ms, desc))

            elif c["type"] == "now_playing":
                desc = f"**{ms.name}** is **now playing** in theatres!\n"
                if ms.release_date:
                    desc += f"\n📅 Release: `{_fmt_date(ms.release_date)}`"
                embeds.append(self._build_embed(f"🎬  Now Playing — {ms.name}", 0x2ECC71, ms, desc))

            else:
                old = c.get("old", "?")
                new = c.get("new", "?")
                desc = f"**{ms.name}** status changed.\n\n"
                desc += f"`{old}` → **`{new}`**"
                if ms.release_date:
                    desc += f"\n📅 Release: `{_fmt_date(ms.release_date)}`"
                embeds.append(self._build_embed(f"🔄  {ms.name} — Status Change", 0x3498DB, ms, desc))

        payload = {"username": DISCORD_USERNAME, "avatar_url": DISCORD_AVATAR, "embeds": embeds[:10]}
        self._discord_post(payload)

    def _discord_post(self, payload: dict) -> None:
        if not self.discord_webhook:
            return
        attempt = 0
        backoff = 0.0
        while True:
            attempt += 1
            try:
                if backoff > 0:
                    time.sleep(backoff)
                    backoff = 0.0
                r = requests.post(self.discord_webhook, json=payload, timeout=30)
                if r.status_code in (200, 204):
                    if attempt > 1:
                        log.info("Discord delivered after %d attempt(s)", attempt)
                    return
                if r.status_code == 429:
                    try:
                        body = r.json()
                    except Exception:
                        body = {}
                    try:
                        retry_after = float(body.get("retry_after", 5))
                    except (TypeError, ValueError):
                        retry_after = 5.0
                    if retry_after <= 0:
                        retry_after = 5.0
                    is_global = body.get("global", False)
                    if attempt == 1 or attempt % 5 == 0:
                        log.warning(
                            "Discord 429 (try %d, global=%s): waiting %.1fs — retry until delivered",
                            attempt,
                            is_global,
                            retry_after,
                        )
                    backoff = retry_after + 0.35
                    continue
                if r.status_code >= 500:
                    backoff = min(120.0, 5.0 * min(attempt, 24))
                    log.warning("Discord %d (try %d): retry in %.0fs", r.status_code, attempt, backoff)
                    continue
                if r.status_code in (400, 401, 403, 404):
                    log.error("Discord HTTP %s (will not retry): %s", r.status_code, (r.text or "")[:800])
                    return
                backoff = min(60.0, 10.0 + attempt)
                log.warning("Discord HTTP %s: retry in %.0fs", r.status_code, backoff)
            except requests.exceptions.Timeout:
                backoff = min(90.0, 5.0 * min(attempt, 18))
                log.warning("Discord timeout (try %d): retry in %.0fs", attempt, backoff)
            except requests.exceptions.ConnectionError as e:
                backoff = min(120.0, 8.0 * min(attempt, 15))
                log.warning("Discord connection error (try %d): retry in %.0fs — %s", attempt, backoff, e)
            except Exception as e:
                backoff = min(60.0, 15.0)
                log.error("Discord send error (try %d): %s — retry in %.0fs", attempt, e, backoff)

    def run(self, oneshot: bool = False):
        self._load()
        log.info(
            "Watcher started | %s | poll %ds",
            ",".join(self.watch_slugs) if self.watch_slugs else "ALL",
            self.poll_interval,
        )
        n = 0
        errs = 0
        while not self._stop:
            n += 1
            t0 = time.monotonic()
            try:
                changes = self.check()
                if changes:
                    for c in changes:
                        log.info("%s: %s", c["movie"].name, c["type"])
                    self._notify(changes)
                self._save()
                errs = 0
                log.info("Watch #%d: %d movies (%.1fs)", n, len(self.movies), time.monotonic() - t0)
            except Exception as e:
                errs += 1
                log.error("Watch #%d error (%d): %s\n%s", n, errs, e, traceback.format_exc())
                if errs >= 5:
                    log.error("Too many consecutive errors — cooling down 60s")
                    time.sleep(60)
                    errs = 0
            if oneshot:
                break
            time.sleep(self.poll_interval)

    def status_report(self) -> str:
        self._load()
        by: Dict[str, List[MovieState]] = {}
        for ms in self.movies.values():
            by.setdefault(ms.status or "?", []).append(ms)
        lines = [
            f"\n{'═' * 80}",
            f"  AMC+ Watcher — {len(self.movies)} movies",
            f"{'═' * 80}",
            "",
        ]
        for s, ms_list in sorted(by.items()):
            lines.append(f"  {s} ({len(ms_list)})")
            for m in sorted(ms_list, key=lambda x: x.release_date or ""):
                sched = " [showtimes]" if m.has_scheduled_showtimes else ""
                lines.append(f"    {m.name:50s} {m.release_date or '?':12s} {m.mpaa_rating or ''}{sched}")
            lines.append("")
        return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=f"AMC+ Watcher v{VERSION}")
    p.add_argument("--movies", "-m", nargs="*")
    p.add_argument("--all", "-a", action="store_true")
    p.add_argument("--theatre", default="amc-lincoln-square-13")
    p.add_argument("--poll", type=int, default=120)
    p.add_argument("--state", default="./state/watcher_state.json")
    p.add_argument("--webhook", default="")
    p.add_argument("--ignore-genres", nargs="*", default=[])
    p.add_argument("--oneshot", "-1", action="store_true")
    p.add_argument("--status", "-s", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, args.log_level.upper(), logging.INFO),
    )
    w = ReleaseWatcher(
        watch_slugs=args.movies,
        watch_all=args.all,
        theatre_slug=args.theatre,
        poll_interval=args.poll,
        state_path=args.state,
        discord_webhook=args.webhook,
        ignore_genres=args.ignore_genres,
    )
    if args.status:
        print(w.status_report())
        return 0
    w.run(oneshot=args.oneshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
