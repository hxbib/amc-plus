from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import random
import signal
import sys
import time
import threading
import traceback
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests
from amc_api import (
    AMCClient,
    Showtime,
    Seat,
    SeatGroup,
    BASE,
    DISCOUNT_DAYS,
    LOVESEAT_TYPES,
    score_seat,
    tier_label,
    parse_seats,
    find_groups,
    non_overlapping,
    seat_sort_key,
    matches_format,
    matches_seating,
    matches_subtitles,
)

log = logging.getLogger("amc+")
VERSION = "5.23.25"
PRODUCT = "AMC+"

_bot_start_time: Optional[datetime] = None
_session_start_mono: float = 0.0
_orders_stats_lock = threading.Lock()
_total_orders_placed_session: int = 0
_session_orders_by_profile = defaultdict(int)
DISCOVERY_HEARTBEAT_INTERVAL = 10


def _reset_session_order_stats() -> None:
    global _total_orders_placed_session
    with _orders_stats_lock:
        _total_orders_placed_session = 0
        _session_orders_by_profile.clear()


def _record_order_placed(profile_id: str) -> None:
    global _total_orders_placed_session
    with _orders_stats_lock:
        _total_orders_placed_session += 1
        _session_orders_by_profile[profile_id] += 1


def _session_order_count(profile_id: str) -> int:
    with _orders_stats_lock:
        return int(_session_orders_by_profile.get(profile_id, 0))


def _total_session_orders() -> int:
    with _orders_stats_lock:
        return int(_total_orders_placed_session)


def _fmt_dt_et_stamp(dt: datetime, tz_str: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_str))
    else:
        dt = dt.astimezone(ZoneInfo(tz_str))
    h12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.strftime('%M:%S')} {ampm} ET · {dt.strftime('%m-%d-%Y')}"


def _format_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts: List[str] = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def _seat_count_from_hold_entry(o: Dict) -> int:
    s = o.get("seats")
    if not s or not isinstance(s, str):
        return 0
    return len([p for p in s.replace("+", ",").split(",") if p.strip()])


def _summarize_showtimes_for_log(found: List[Showtime], limit: int = 8) -> str:
    if not found:
        return ""
    lines = []
    for st in sorted(found, key=lambda s: (s.date, s.time))[:limit]:
        fl = st.format_label if hasattr(st, "format_label") else ""
        lines.append(f"{st.label} sid={st.sid} {fl}".strip())
    if len(found) > limit:
        lines.append(f"… +{len(found) - limit} more")
    return " | ".join(lines)


def _discord_mode_release_to_snipe(
    cfg: Cfg,
    pr: ProfileRuntime,
    *,
    orders_held: int,
    release_elapsed_sec: float,
    source: str,
    showtimes_in_release: Optional[int] = None,
) -> None:
    title = f"[MODE] Release → Snipe — {pr.movie_name or pr.movie}"
    cap = max(0, pr.max_orders - orders_held)
    desc = (
        f"**{pr.movie_name or pr.movie}** is now in **snipe** mode.\n"
        f"`source` — `{source}`\n"
        f"**Orders placed (held now):** `{orders_held}` / `{pr.max_orders}` · **Capacity remaining:** `{cap}`\n"
        f"**Release window duration:** `{_format_duration(release_elapsed_sec)}`"
    )
    embed = _base_embed(title, 0x9B59B6, description=desc, url=_amc_movie_link(pr), pr=pr)
    embed["fields"] = [
        {"name": "Original mode", "value": f"`{pr.original_mode}`", "inline": True},
        {"name": "Current mode", "value": "`snipe`", "inline": True},
        {"name": "Auto-Reserve", "value": f"`{'On' if pr.auto_reserve else 'Off'}`", "inline": True},
    ]
    if showtimes_in_release is not None:
        embed["fields"].append(
            {"name": "Showtimes scanned", "value": f"`{showtimes_in_release}`", "inline": True},
        )
    _send_discord(cfg, {"embeds": [embed]})


TZ = "America/New_York"
STATE_PATH = "./state/amc_state.json"
DISCORD_USERNAME = "AMC+"
DISCORD_AVATAR = "https://i.imgur.com/Pll0t9Y.jpeg"
FOOTER_ICON = "https://i.imgur.com/Pll0t9Y.jpeg"
AUTHOR_ICON = "https://i.imgur.com/Pll0t9Y.jpeg"
AUTHOR_URL = "https://www.amctheatres.com/"
TMDB_API_KEY = "thisismyyytmdbapikeygetyourown"
TMDB_READ_ACCESS_TOKEN = (
    "getyourownaccesskey.nope"
)
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"
AMC_MOVIE_URL = "https://www.amctheatres.com/movies"


@dataclass
class DiscordCfg:
    webhook_url: str = ""


@dataclass
class ProfileCfg:

    movie: str = ""
    theatre: str = ""
    theatres: Optional[List[str]] = None
    format: str = ""
    seating: str = ""
    subtitles: str = ""
    mode: str = ""
    quantity: Optional[int] = None
    tier: str = ""
    target_seats: Optional[List[str]] = None
    min_score: Optional[float] = None
    target_dates: Optional[List[str]] = None
    discount: str = ""
    scan_days: Optional[int] = None
    poll: Optional[int] = None
    post_showtime_minutes: Optional[int] = None
    discovery_interval: Optional[int] = None
    workers: Optional[int] = None
    timeout: Optional[int] = None
    auto_reserve: Optional[bool] = None
    max_orders: Optional[int] = None
    suppress_first: Optional[bool] = None
    alert_cooldown_minutes: Optional[int] = None
    ticket_presale_start: Optional[str] = None
    early_drop_check: Optional[bool] = None


def _parse_discount(raw: Dict, fallback: str = "off") -> str:
    if "discount" in raw:
        v = str(raw["discount"]).lower().strip()
        if v in ("true", "require", "yes", "1"):
            return "require"
        if v in ("days", "days_only"):
            return "days"
        if v in ("", "off", "false", "no", "0"):
            return "off"
        return "off"
    do = str(raw.get("discount_only", False)).lower() in ("true", "1", "yes")
    rd = str(raw.get("require_discount", False)).lower() in ("true", "1", "yes")
    if rd:
        return "require"
    if do:
        return "days"
    return fallback


@dataclass
class Cfg:

    movie: str = ""
    theatre: str = "amc-lincoln-square-13"
    theatres: List[str] = field(default_factory=list)
    format: str = ""
    seating: str = ""
    subtitles: str = ""
    mode: str = "snipe"
    quantity: int = 2
    tier: str = "ELITE"
    target_seats: List[str] = field(default_factory=list)
    min_score: float = 0
    target_dates: List[str] = field(default_factory=list)
    discount: str = "off"
    scan_days: int = 45
    poll: int = 5
    post_showtime_minutes: int = 20
    discovery_interval: int = 30
    timeout: int = 12
    workers: int = 32
    auto_reserve: bool = True
    max_orders: int = 3
    suppress_first: bool = True
    alert_cooldown_minutes: int = 45
    early_drop_check: bool = True

    discord: DiscordCfg = field(default_factory=DiscordCfg)
    state_path: str = STATE_PATH
    tz: str = TZ
    profiles: List[ProfileCfg] = field(default_factory=list)

    movie_slug: str = ""
    movie_name: str = ""
    movie_id: int = 0
    theatre_name: str = ""
    premium_label: str = ""
    movie_image: str = ""
    _resolved_dates: Set[str] = field(default_factory=set)

    @staticmethod
    def from_json(path: str) -> "Cfg":
        import re as _re

        with open(path) as f:
            raw = f.read()
        raw = _re.sub(r"(?<!:)//.*", "", raw)
        r = json.loads(raw)

        def g(key, *alts, default=None):
            for k in (key, *alts):
                if k in r:
                    return r[k]
            return default

        dc = r.get("discord", {})
        cfg = Cfg(
            movie=os.environ.get("AMC_MOVIE", g("movie", "movie_slug", default="")),
            theatre=os.environ.get(
                "AMC_THEATRE",
                g("theatre", "theatre_slug", default="amc-lincoln-square-13"),
            ),
            theatres=g("theatres", default=[]),
            format=os.environ.get(
                "AMC_FORMAT", g("format", "premium", "premium_offering", default="")
            ),
            seating=g("seating", "seating_type", default=""),
            subtitles=g("subtitles", default=""),
            mode=os.environ.get("AMC_MODE", g("mode", default="snipe")).lower(),
            quantity=int(os.environ.get("AMC_QTY", g("quantity", "qty", "group_size", default=2))),
            tier=os.environ.get("AMC_TIER", g("tier", "tier_filter", default="ELITE")).upper(),
            target_seats=g("target_seats", "seats", default=[]),
            min_score=float(g("min_score", default=0)),
            target_dates=g("target_dates", "dates", default=[]),
            discount=_parse_discount(r),
            scan_days=max(1, int(g("scan_days", default=45))),
            poll=max(1, int(os.environ.get("AMC_POLL", g("poll", "poll_interval_seconds", default=5)))),
            post_showtime_minutes=max(0, int(g("post_showtime_minutes", "grace_minutes", default=20))),
            discovery_interval=max(5, int(g("discovery_interval", default=30))),
            timeout=(
                _to
                if (_to := int(g("timeout", "request_timeout_seconds", default=12))) > 0
                else 12
            ),
            workers=max(1, int(g("workers", "max_workers", default=32))),
            auto_reserve=bool(g("auto_reserve", default=True)),
            max_orders=max(1, int(g("max_orders", default=3))),
            suppress_first=bool(g("suppress_first", default=True)),
            alert_cooldown_minutes=max(1, int(g("alert_cooldown_minutes", default=45))),
            early_drop_check=bool(g("early_drop_check", default=True)),
            discord=DiscordCfg(
                webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", dc.get("webhook_url", "")),
            ),
            state_path=STATE_PATH,
            tz=TZ,
        )

        raw_profiles = r.get("profiles") or []
        for pd in raw_profiles:
            p = ProfileCfg(
                movie=pd.get("movie", ""),
                theatre=pd.get("theatre", ""),
                theatres=pd.get("theatres"),
                format=pd.get("format", ""),
                seating=pd.get("seating", ""),
                subtitles=pd.get("subtitles", ""),
                mode=pd.get("mode", ""),
                quantity=pd.get("quantity"),
                tier=pd.get("tier", ""),
                target_seats=pd.get("target_seats"),
                min_score=pd.get("min_score"),
                target_dates=pd.get("target_dates"),
                discount=_parse_discount(pd, fallback=""),
                scan_days=pd.get("scan_days"),
                poll=pd.get("poll"),
                post_showtime_minutes=pd.get("post_showtime_minutes"),
                discovery_interval=pd.get("discovery_interval"),
                workers=pd.get("workers"),
                timeout=pd.get("timeout"),
                auto_reserve=pd.get("auto_reserve"),
                max_orders=pd.get("max_orders"),
                suppress_first=pd.get("suppress_first"),
                alert_cooldown_minutes=pd.get("alert_cooldown_minutes"),
                ticket_presale_start=(pd.get("ticket_presale_start") or pd.get("presale_start") or None),
                early_drop_check=pd.get("early_drop_check"),
            )
            cfg.profiles.append(p)

        return cfg

    @staticmethod
    def expand_dates(dates: List[str]) -> Set[str]:
        resolved: Set[str] = set()
        for d in dates:
            if ":" in d:
                parts = d.split(":")
                start = datetime.strptime(parts[0], "%Y-%m-%d")
                end = datetime.strptime(parts[1], "%Y-%m-%d")
                cur = start
                while cur <= end:
                    resolved.add(cur.strftime("%Y-%m-%d"))
                    cur += timedelta(days=1)
            else:
                resolved.add(d)
        return resolved

    def build_profile_configs(self) -> List["ProfileRuntime"]:
        if self.profiles:
            return [ProfileRuntime.from_cfg_and_profile(self, p) for p in self.profiles]
        if self.movie:
            p = ProfileCfg(movie=self.movie)
            return [ProfileRuntime.from_cfg_and_profile(self, p)]
        return []


@dataclass
class ProfileRuntime:

    profile_id: str = ""
    movie: str = ""
    movie_slug: str = ""
    movie_name: str = ""
    movie_id: int = 0
    movie_image: str = ""
    release_date: str = ""
    ticket_avail_date: str = ""
    run_time_minutes: int = 0
    ticket_presale_start: str = ""

    format: str = ""
    seating: str = ""
    subtitles: str = ""
    mode: str = "snipe"
    original_mode: str = "snipe"
    release_report_sent: bool = False
    quantity: int = 2
    tier: str = "ELITE"
    target_seats: List[str] = field(default_factory=list)
    min_score: float = 0
    discount: str = "off"
    auto_reserve: bool = True
    max_orders: int = 3
    scan_days: int = 45
    poll: int = 5
    post_showtime_minutes: int = 20
    discovery_interval: int = 30
    workers: int = 32
    timeout: int = 12
    suppress_first: bool = True
    alert_cooldown_minutes: int = 45
    early_drop_check: bool = True
    premium_label: str = ""

    theatres: List[str] = field(default_factory=list)
    theatre_ids: Dict[str, int] = field(default_factory=dict)
    theatre_names: Dict[str, str] = field(default_factory=dict)
    theatre_addresses: Dict[str, str] = field(default_factory=dict)
    theatre_name: str = ""

    target_dates: List[str] = field(default_factory=list)
    resolved_dates: Set[str] = field(default_factory=set)
    state_key: str = ""

    @staticmethod
    def from_cfg_and_profile(cfg: Cfg, p: ProfileCfg) -> "ProfileRuntime":
        theatres = list(p.theatres) if p.theatres is not None else list(cfg.theatres)
        if p.theatre:
            if p.theatre not in theatres:
                theatres.insert(0, p.theatre)
        elif cfg.theatre and cfg.theatre not in theatres:
            theatres.insert(0, cfg.theatre)
        if not theatres:
            if p.theatre:
                theatres = [p.theatre]
            elif cfg.theatre:
                theatres = [cfg.theatre]

        target_dates = list(p.target_dates) if p.target_dates is not None else list(cfg.target_dates)
        pid = p.movie.replace(" ", "-").lower()[:30] or uuid.uuid4().hex[:8]
        _mode = (p.mode or cfg.mode).lower()

        return ProfileRuntime(
            profile_id=pid,
            movie=p.movie,
            format=p.format or cfg.format,
            seating=p.seating or cfg.seating,
            subtitles=p.subtitles if p.subtitles else cfg.subtitles,
            mode=_mode,
            original_mode=_mode,
            quantity=max(1, p.quantity if p.quantity is not None else cfg.quantity),
            tier=(p.tier or cfg.tier).upper(),
            target_seats=list(p.target_seats) if p.target_seats is not None else list(cfg.target_seats),
            min_score=p.min_score if p.min_score is not None else cfg.min_score,
            discount=p.discount or cfg.discount,
            auto_reserve=p.auto_reserve if p.auto_reserve is not None else cfg.auto_reserve,
            max_orders=max(1, p.max_orders if p.max_orders is not None else cfg.max_orders),
            scan_days=max(1, p.scan_days if p.scan_days is not None else cfg.scan_days),
            poll=max(1, p.poll if p.poll is not None else cfg.poll),
            post_showtime_minutes=max(0, p.post_showtime_minutes if p.post_showtime_minutes is not None else cfg.post_showtime_minutes),
            discovery_interval=max(5, p.discovery_interval if p.discovery_interval is not None else cfg.discovery_interval),
            workers=max(1, p.workers if p.workers is not None else cfg.workers),
            timeout=max(1, p.timeout if p.timeout is not None else cfg.timeout),
            suppress_first=p.suppress_first if p.suppress_first is not None else cfg.suppress_first,
            alert_cooldown_minutes=max(1, p.alert_cooldown_minutes if p.alert_cooldown_minutes is not None else cfg.alert_cooldown_minutes),
            early_drop_check=p.early_drop_check if p.early_drop_check is not None else cfg.early_drop_check,
            ticket_presale_start=(p.ticket_presale_start or "").strip(),
            theatres=theatres,
            target_dates=target_dates,
            state_key=f"profile.{pid}",
        )

    def score_threshold(self) -> float:
        if self.min_score > 0:
            return self.min_score
        t = self.tier.split()[-1] if self.tier else "ELITE"
        return {"ELITE": 80, "GREAT": 70, "GOOD": 60, "OK": 55, "ALL": 0}.get(t, 55)

    def passes_tier(self, score: float) -> bool:
        return score >= self.score_threshold()

    def date_passes(self, date_str: str) -> bool:
        if self.resolved_dates and date_str not in self.resolved_dates:
            return False
        if self.discount in ("days", "require"):
            if datetime.strptime(date_str, "%Y-%m-%d").weekday() not in DISCOUNT_DAYS:
                return False
        return True

    def discount_filter(self, st: Showtime) -> bool:
        if self.discount != "require":
            return True
        if st.discount_excluded:
            return False
        if st.discount_eligible is False:
            return False
        return True


def _now(tz_str: str) -> datetime:
    return datetime.now(ZoneInfo(tz_str))


HOLD_PRUNE_AFTER_MINUTES = 10.0


def _hold_created_dt(raw: Any, tz: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        s = str(raw).strip()
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt
    except (ValueError, TypeError):
        return None


def _prune_stale_holds(held_orders: List[Dict], tz: str) -> int:
    if not held_orders:
        return 0
    cutoff = _now(tz) - timedelta(minutes=HOLD_PRUNE_AFTER_MINUTES)
    kept: List[Dict] = []
    removed = 0
    for o in held_orders:
        dt = _hold_created_dt(o.get("created"), tz)
        if dt is None:
            kept.append(o)
            continue
        if dt < cutoff:
            removed += 1
            log.info(
                "Pruned stale hold (>%d min): seats=%r sid=%s",
                int(HOLD_PRUNE_AFTER_MINUTES),
                o.get("seats"),
                o.get("sid"),
            )
        else:
            kept.append(o)
    if removed:
        held_orders[:] = kept
    return removed


def _dt(st: Showtime, tz: str) -> datetime:
    if st.show_datetime_local:
        try:
            return datetime.fromisoformat(st.show_datetime_local).replace(tzinfo=ZoneInfo(tz))
        except (ValueError, TypeError):
            pass
    y, mo, d = (int(x) for x in st.date.split("-"))
    t = datetime.strptime(f"{st.time} {st.ampm}", "%I:%M %p")
    return datetime(y, mo, d, t.hour, t.minute, tzinfo=ZoneInfo(tz))


def _ts() -> str:
    return datetime.now(tz=ZoneInfo(TZ)).isoformat(timespec="seconds")


def _footer_stamp() -> str:
    dt = datetime.now(tz=ZoneInfo(TZ))
    return dt.strftime("%m-%d-%Y %I:%M:%S %p %Z")


def _fmt_date(iso: str) -> str:
    try:
        y, m, d = iso.split("-")
        return f"{m}-{d}-{y}"
    except Exception:
        return iso


def _fmt_time(time_str: str, ampm: str) -> str:
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        h, m = parts
        return f"{int(h)}:{m} {ampm.upper().strip()}"
    return f"{time_str} {ampm}"


def _fmt_showtime_display(date_iso: str, time_str: str, ampm: str) -> str:
    return f"{_fmt_date(date_iso)} {_fmt_time(time_str, ampm)}"


def _theatre_address_lines(flat: Dict[str, str]) -> str:
    if not flat:
        return ""
    a1 = (flat.get("addressLine1") or "").strip()
    city = (flat.get("city") or "").strip()
    st = (flat.get("stateCode") or "").strip()
    z = (flat.get("postalCode") or "").strip()
    line2 = " ".join(p for p in [f"{city}, {st}".strip(", ") if city else st, z] if p)
    if a1 and line2:
        return f"{a1}\n{line2}"
    return a1 or line2


def _format_runtime_minutes(mins: int) -> str:
    if not mins or mins < 1:
        return ""
    h, m = divmod(int(mins), 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


_tmdb_cache: Dict[str, str] = {}


def _tmdb_search_poster(query: str) -> str:
    if not query:
        return ""
    if query in _tmdb_cache:
        return _tmdb_cache[query]
    try:
        headers = {"Authorization": f"Bearer {TMDB_READ_ACCESS_TOKEN}"}
        r = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"query": query, "page": 1},
            headers=headers,
            timeout=6,
        )
        if r.status_code == 401:
            r = requests.get(
                "https://api.themoviedb.org/3/search/movie",
                params={"api_key": TMDB_API_KEY, "query": query, "page": 1},
                timeout=6,
            )
        if r.ok:
            results = r.json().get("results", [])
            if results and results[0].get("poster_path"):
                url = f"{TMDB_IMG_BASE}{results[0]['poster_path']}"
                _tmdb_cache[query] = url
                return url
    except Exception:
        pass
    _tmdb_cache[query] = ""
    return ""


def _get_poster(pr: "ProfileRuntime") -> str:
    if pr.movie_image:
        return pr.movie_image
    key = pr.movie_name or pr.movie_slug or pr.movie
    return _tmdb_search_poster(key)


def _slug_to_readable(slug: str) -> str:
    if not slug:
        return ""
    parts = slug.replace("_", "-").split("-")
    words: List[str] = []
    for p in parts:
        if not p:
            continue
        if p.isdigit() and len(p) >= 3:
            continue
        words.append(p[:1].upper() + p[1:].lower() if len(p) > 1 else p.upper())
    return " ".join(words) if words else slug


def _fmt_hold_expires_et(cfg: Cfg) -> str:
    dt = _now(cfg.tz) + timedelta(minutes=8)
    h12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.strftime('%M')} {ampm} ET"


def _amc_movie_link(pr: "ProfileRuntime") -> str:
    slug = pr.movie_slug or pr.movie
    if slug:
        return f"{AMC_MOVIE_URL}/{slug}"
    return AMC_MOVIE_URL


def _parse_release_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _profile_phase(pr: "ProfileRuntime", tz: str) -> str:
    rd = _parse_release_date(pr.release_date)
    if not rd:
        return "active"
    today = _now(tz).date()
    days_until = (rd - today).days
    ps = _parse_release_date(pr.ticket_presale_start)

    if ps and today < ps:
        return "dormant"
    if not ps and days_until > 56:
        return "dormant"

    if days_until <= 14:
        return "active"
    if days_until <= 56:
        return "watch"
    if ps and today >= ps:
        return "watch"
    return "dormant"


def _compute_smart_dates(pr: "ProfileRuntime", tz: str) -> Tuple[str, List[str]]:
    today = _now(tz).date()
    today_str = today.strftime("%Y-%m-%d")

    if pr.resolved_dates:
        valid = sorted(d for d in pr.resolved_dates if d >= today_str)
        return "active", valid or sorted(pr.resolved_dates)

    rd = _parse_release_date(pr.release_date)
    ps = _parse_release_date(pr.ticket_presale_start)
    if not rd:
        all_d = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(pr.scan_days, 1))]
        return "active", [d for d in all_d if pr.date_passes(d)] or all_d

    days_until = (rd - today).days

    if ps and today < ps:
        return "dormant", []
    if not ps and days_until > 56:
        return "dormant", []

    if days_until > 14:
        start = max(today, rd - timedelta(days=3))
        end = rd + timedelta(days=7)
        if ps and today >= ps and days_until > 56:
            start = today
            end = rd + timedelta(days=21)
        dates = []
        d = start
        while d <= end:
            dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        return "watch", dates

    if days_until > 0:
        start = max(today, rd - timedelta(days=7))
        end = rd + timedelta(days=21)
        dates = []
        d = start
        while d <= end:
            ds = d.strftime("%Y-%m-%d")
            if pr.date_passes(ds):
                dates.append(ds)
            d += timedelta(days=1)
        return "active", dates or [(rd + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(-7, 22)]

    end = rd + timedelta(days=max(pr.scan_days, 28))
    dates = []
    d = today
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        if pr.date_passes(ds):
            dates.append(ds)
        d += timedelta(days=1)
    return "active", dates or [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(pr.scan_days)]


def resolve_profile(pr: ProfileRuntime, client: AMCClient, cfg: Cfg):
    cid = uuid.uuid4().hex[:6]
    log.info("[%s] Resolving profile: movie=%r theatres=%s", cid, pr.movie, pr.theatres)

    if pr.movie:
        try:
            m = client.resolve_movie(pr.movie)
            if m:
                pr.movie_slug = m.get("slug", "")
                pr.movie_name = m.get("name", pr.movie)
                pr.movie_id = m.get("id", 0)
                pr.movie_image = m.get("poster_url", "")
                pr.release_date = m.get("release_date", "")
                pr.ticket_avail_date = m.get("ticket_avail_date", "")
                pr.run_time_minutes = int(m.get("run_time") or 0)
                rd = _parse_release_date(pr.release_date)
                log.info("[%s] Movie resolved: %s → id=%s slug=%s release=%s", cid, pr.movie, pr.movie_id, pr.movie_slug, rd)
            else:
                log.error("[%s] Could not resolve movie: %r", cid, pr.movie)
                pr.movie_slug = pr.movie
        except Exception as e:
            log.error("[%s] Movie resolution failed for %r: %s", cid, pr.movie, e)
            pr.movie_slug = pr.movie

    resolved_theatres: List[str] = []
    for th in pr.theatres:
        try:
            t = client.resolve_theatre(th)
            if t:
                slug = t.get("slug", th)
                resolved_theatres.append(slug)
                pr.theatre_ids[slug] = int(t.get("id", 0))
                pr.theatre_names[slug] = t.get("name", slug)
                addr = _theatre_address_lines(t)
                if addr:
                    pr.theatre_addresses[slug] = addr
                log.info("[%s] Theatre resolved: %s → id=%d name=%s", cid, th, pr.theatre_ids[slug], pr.theatre_names[slug])
            else:
                log.warning("[%s] Could not resolve theatre: %r — keeping raw", cid, th)
                resolved_theatres.append(th)
        except Exception as e:
            log.error("[%s] Theatre resolution failed for %r: %s", cid, th, e)
            resolved_theatres.append(th)

    pr.theatres = resolved_theatres
    if pr.theatres:
        pr.theatre_name = pr.theatre_names.get(pr.theatres[0], pr.theatres[0])
    pr.premium_label = pr.format.upper() if pr.format else "All Formats"
    pr.resolved_dates = Cfg.expand_dates(pr.target_dates)


def resolve_config(cfg: Cfg, client: AMCClient):
    if cfg.movie:
        m = client.resolve_movie(cfg.movie)
        if m:
            cfg.movie_slug = m.get("slug", "")
            cfg.movie_name = m.get("name", cfg.movie)
            cfg.movie_id = m.get("id", 0)
            cfg.movie_image = m.get("poster_url", "")
            log.info("Movie: %s → %s (id=%s)", cfg.movie, cfg.movie_slug, cfg.movie_id)
        else:
            log.error("Could not resolve movie: %s", cfg.movie)
            cfg.movie_slug = cfg.movie
    if cfg.theatre:
        t = client.resolve_theatre(cfg.theatre)
        if t:
            cfg.theatre = t.get("slug", cfg.theatre)
            cfg.theatre_name = t.get("name", cfg.theatre)
    resolved_theatres = []
    for th in cfg.theatres:
        t = client.resolve_theatre(th)
        resolved_theatres.append(t.get("slug", th) if t else th)
    cfg.theatres = resolved_theatres
    if cfg.theatre and cfg.theatre not in cfg.theatres:
        cfg.theatres.insert(0, cfg.theatre)
    if not cfg.theatres and cfg.theatre:
        cfg.theatres = [cfg.theatre]
    cfg.premium_label = cfg.format.upper() if cfg.format else "All Formats"
    cfg._resolved_dates = Cfg.expand_dates(cfg.target_dates)


def discover_showtimes(
    pr: ProfileRuntime,
    client: AMCClient,
    cfg: Cfg,
    *,
    seating_attr_probe: str = "",
    _dates_override: Optional[List[str]] = None,
) -> List[Showtime]:
    now = _now(cfg.tz)
    grace = timedelta(minutes=pr.post_showtime_minutes)
    phase, dates = _compute_smart_dates(pr, cfg.tz)
    if _dates_override is not None:
        dates = _dates_override
        phase = "early_drop"
    if not dates:
        log.info(
            "[%s] Discovery window empty (phase=%s, movie_id=%s) — no dates to query",
            pr.profile_id,
            phase,
            pr.movie_id or 0,
        )
        return []

    d_first, d_last = dates[0], dates[-1]
    work_items = []
    for th_slug in pr.theatres:
        th_id = pr.theatre_ids.get(th_slug, 0)
        if not th_id:
            log.warning("No theatre ID for %s — skipping", th_slug)
            continue
        for d in dates:
            work_items.append((th_slug, th_id, d))

    log.info(
        "[%s] Discovery start phase=%s dates=%s..%s (%d days) work_items=%d movie_id=%s format=%s",
        pr.profile_id,
        phase,
        d_first,
        d_last,
        len(dates),
        len(work_items),
        pr.movie_id or 0,
        pr.format or "ALL",
    )

    seen_sids: Set[int] = set()
    raw_showtimes: List[Dict] = []
    fetch_errors: List[Tuple[str, str, str]] = []

    def fetch(item):
        th_slug, th_id, d = item
        try:
            results = client.get_showtimes(
                th_id, d, movie_id=pr.movie_id or None, theatre_slug=th_slug
            )
            return th_slug, th_id, d, results, None
        except Exception as e:
            return th_slug, th_id, d, [], e

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(pr.workers, len(work_items) or 1)) as ex:
        for th_slug, th_id, d, results, err in ex.map(fetch, work_items):
            if err:
                fetch_errors.append((th_slug, d, str(err)))
                log.debug("Discovery fetch fail %s %s: %s", th_slug, d, err)
                continue
            for st_data in results:
                sid = st_data.get("id", 0)
                if not sid or sid in seen_sids:
                    continue
                seen_sids.add(sid)
                st_data["_theatre_slug"] = th_slug
                st_data["_theatre_id"] = th_id
                st_data["_date"] = d
                raw_showtimes.append(st_data)

    sids_needing_attrs = []
    rest_showtimes: List[Showtime] = []

    for st_data in raw_showtimes:
        th_slug = st_data.get("_theatre_slug", "")
        st = Showtime.from_rest(
            st_data,
            pr.theatre_names.get(th_slug, th_slug),
            theatre_slug=th_slug,
        )
        if st.status == "CANCELED" or st.is_sold_out:
            continue
        if pr.movie_id and st.movie_id and st.movie_id != pr.movie_id:
            continue
        rest_showtimes.append(st)
        if not st.attribute_codes:
            sids_needing_attrs.append(st.sid)
        elif (
            seating_attr_probe
            and not matches_seating(
                st.format_attrs, seating_attr_probe, st.attribute_codes
            )
        ):
            sids_needing_attrs.append(st.sid)

    if sids_needing_attrs:
        for i in range(0, len(sids_needing_attrs), 20):
            chunk = sids_needing_attrs[i : i + 20]
            try:
                attrs_map = client.get_showtime_attrs_batch(chunk)
                for st in rest_showtimes:
                    if st.sid in attrs_map:
                        ad = attrs_map[st.sid]
                        attr_edges = (ad.get("attributes") or {}).get("edges") or []
                        new_codes = [
                            e["node"].get("code", "")
                            for e in attr_edges
                            if e.get("node", {}).get("code")
                        ]
                        new_names = [
                            e["node"].get("name", "")
                            for e in attr_edges
                            if e.get("node", {}).get("name")
                        ]
                        st.attribute_codes = list(
                            dict.fromkeys(
                                [c for c in st.attribute_codes if c] + new_codes
                            )
                        )
                        st.format_attrs = list(
                            dict.fromkeys(
                                [n for n in st.format_attrs if n] + new_names
                            )
                        )
                        if ad.get("auditorium"):
                            st.auditorium = ad["auditorium"]
                        if ad.get("isDiscountDaysEligible") is not None:
                            st.discount_eligible = ad["isDiscountDaysEligible"]
            except Exception as e:
                log.warning("GQL attrs batch enrichment failed: %s", e)

    out: List[Showtime] = []
    for st in rest_showtimes:
        if pr.format and not matches_format(st.format_attrs, pr.format, st.attribute_codes):
            continue
        if pr.seating and not matches_seating(st.format_attrs, pr.seating, st.attribute_codes):
            continue
        if not matches_subtitles(st.format_attrs, pr.subtitles, st.attribute_codes, st.movie_name):
            continue
        if not pr.discount_filter(st):
            continue
        try:
            if _dt(st, cfg.tz) + grace <= now:
                continue
        except Exception:
            pass
        out.append(st)

    out.sort(key=lambda s: _dt(s, cfg.tz))
    elapsed = time.monotonic() - t0
    if fetch_errors:
        nfe = len(fetch_errors)
        sample = "; ".join(f"{a} {b}: {c[:120]}" for a, b, c in fetch_errors[:3])
        log.warning(
            "[%s] Discovery: %d/%d theatre×date fetches failed (see DEBUG for all). Sample: %s",
            pr.profile_id,
            nfe,
            len(work_items),
            sample,
        )
    log.info(
        "Discovery [%s]: %d showtimes (%d theatres, %d dates, format=%s) in %.1fs",
        pr.movie_slug or pr.movie, len(out), len(pr.theatres), len(dates),
        pr.format or "ALL", elapsed,
    )
    return out


def discover_showtimes_legacy(cfg: Cfg, client: AMCClient) -> List[Showtime]:
    pr = ProfileRuntime(
        movie=cfg.movie, movie_slug=cfg.movie_slug, movie_name=cfg.movie_name,
        movie_id=cfg.movie_id, movie_image=cfg.movie_image,
        format=cfg.format, seating=cfg.seating, subtitles=cfg.subtitles,
        mode=cfg.mode, quantity=cfg.quantity, tier=cfg.tier,
        target_seats=cfg.target_seats, min_score=cfg.min_score,
        discount=cfg.discount, auto_reserve=cfg.auto_reserve, max_orders=cfg.max_orders,
        scan_days=cfg.scan_days, poll=cfg.poll, workers=cfg.workers,
        post_showtime_minutes=cfg.post_showtime_minutes,
        discovery_interval=cfg.discovery_interval, timeout=cfg.timeout,
        theatres=list(cfg.theatres), target_dates=list(cfg.target_dates),
        resolved_dates=set(cfg._resolved_dates),
    )
    for th in pr.theatres:
        t = client.resolve_theatre(th)
        if t:
            pr.theatre_ids[t.get("slug", th)] = int(t.get("id", 0))
            pr.theatre_names[t.get("slug", th)] = t.get("name", th)
    return discover_showtimes(pr, client, cfg)


def _needs_loveseat_enforcement(pr: ProfileRuntime, seats: list) -> bool:
    return (
        pr.seating.lower() in ("recliners", "recliner")
        and pr.quantity % 2 == 0
        and any(s.stype in LOVESEAT_TYPES for s in seats)
    )


def check_seats(pr: ProfileRuntime, client: AMCClient, st: Showtime):
    try:
        raw = client.get_seat_layout(st.sid)
    except Exception as e:
        log.debug("Seat layout SID %d failed: %s", st.sid, e)
        return [], []
    layout = (raw or {}).get("seatingLayout")
    if not layout:
        return [], []
    seats, rows, cols = parse_seats(layout)
    require_lp = _needs_loveseat_enforcement(pr, seats)
    good = [s for s in seats if pr.passes_tier(s.score)]
    if pr.target_seats:
        ts = {n.upper() for n in pr.target_seats}
        good = [s for s in good if s.name.upper() in ts]
    groups = find_groups(st.sid, seats, rows, cols, pr.quantity, require_loveseat_pairs=require_lp)
    gg = [g for g in groups if pr.passes_tier(g.score)]
    if pr.target_seats:
        ts = {n.upper() for n in pr.target_seats}
        gg = [g for g in gg if all(s.name.upper() in ts for s in g.seats)]
    return good, gg


def _check_seats_aggressive(pr: ProfileRuntime, client: AMCClient, st: Showtime):
    try:
        raw = client.get_seat_layout(st.sid)
    except Exception as e:
        log.debug("Seat layout SID %d failed: %s", st.sid, e)
        return [], []
    layout = (raw or {}).get("seatingLayout")
    if not layout:
        return [], []
    seats, rows, cols = parse_seats(layout)
    require_lp = _needs_loveseat_enforcement(pr, seats)
    groups = find_groups(st.sid, seats, rows, cols, pr.quantity, require_loveseat_pairs=require_lp)
    if pr.target_seats:
        ts = {n.upper() for n in pr.target_seats}
        groups = [g for g in groups if all(s.name.upper() in ts for s in g.seats)]
        seats = [s for s in seats if s.name.upper() in ts]
    return seats, groups


def release_mode(
    pr: ProfileRuntime,
    client: AMCClient,
    cfg: Cfg,
    showtimes: List[Showtime],
    held_orders: List[Dict],
    oneshot: bool = False,
    *,
    alert_tag: str = "RELEASE DAY",
    release_context: Optional[Dict[str, Any]] = None,
) -> int:
    _prune_stale_holds(held_orders, cfg.tz)
    orders_before_fire = len(held_orders)
    log.info("[%s] Release mode: pre-scoring %d showtimes (best seats, ignoring tier)...", pr.profile_id, len(showtimes))
    pre: Dict[int, List[SeatGroup]] = {}
    for st in showtimes:
        try:
            _, groups = _check_seats_aggressive(pr, client, st)
            top = non_overlapping(groups, 3)
            if top:
                pre[st.sid] = top
                log.info("  %s → best: %s (%.1f)", st.label, top[0].names, top[0].score)
        except Exception as e:
            log.debug("Pre-score %d: %s", st.sid, e)
    if not pre:
        log.warning("[%s] No seats to target", pr.profile_id)
        return 0

    fire = [(sid, pre[sid][0].coords) for sid in pre]
    fire_meta = {sid: pre[sid][0] for sid in pre}
    st_map = {st.sid: st for st in showtimes}

    log.info(
        "RELEASE_FLOW [%s] RELEASE_PRE_FIRE showtimes=%d batch_targets=%d alert_tag=%s poll=%ds",
        pr.profile_id,
        len(showtimes),
        len(fire),
        alert_tag,
        pr.poll,
    )
    log.info("[%s] Speculative fire: %d orders (poll=%dms)", pr.profile_id, len(fire), pr.poll * 1000)
    scan = 0
    first_order_mono: Optional[float] = None
    detection_mono = (release_context or {}).get("detection_mono")
    while True:
        _prune_stale_holds(held_orders, cfg.tz)
        scan += 1
        t0 = time.monotonic()
        try:
            results = client.create_order_batch(fire)
        except Exception as e:
            log.error("[%s] Batch order: %s", pr.profile_id, e)
            time.sleep(pr.poll)
            continue
        batch_ms = (time.monotonic() - t0) * 1000
        log.info(
            "RELEASE_FLOW [%s] RELEASE_BATCH_CYCLE cycle=%d batch_ms=%.0f pending_targets=%d",
            pr.profile_id,
            scan,
            batch_ms,
            len(fire),
        )
        no_token_meta: List[Tuple[int, Any]] = []
        for i, r in enumerate(results):
            if r.get("token"):
                sid = fire[i][0]
                g = fire_meta[sid]
                st = st_map.get(sid)
                entry = {
                    "token": r["token"], "sid": sid, "seats": g.names, "score": g.score,
                    "date": st.date if st else "", "time": f"{st.time} {st.ampm}" if st else "",
                    "movie": pr.movie_name or pr.movie_slug,
                    "url": f"{BASE}/orders/{r['token']}/food-and-drink",
                    "total": r.get("total", ""), "created": _now(cfg.tz).isoformat(),
                }
                held_orders.append(entry)
                _record_order_placed(pr.profile_id)
                _send_alert(cfg, pr, st, g, entry, alert_tag)
                log.info("[%s] RESERVED %s → %s", pr.profile_id, g.names, entry["url"])
                if first_order_mono is None:
                    first_order_mono = time.monotonic()
            else:
                no_token_meta.append((fire[i][0], r))
        if no_token_meta:
            sid0, r0 = no_token_meta[0]
            keys0 = list(r0.keys()) if isinstance(r0, dict) else type(r0).__name__
            log.warning(
                "[%s] Release cycle %d: %d/%d batch slots had no token (e.g. sid=%s keys=%s)",
                pr.profile_id,
                scan,
                len(no_token_meta),
                len(results),
                sid0,
                keys0,
            )
        successes = {fire[i][0] for i, r in enumerate(results) if r.get("token")}
        if successes:
            fire = [(s, c) for s, c in fire if s not in successes]
            if not fire or len(held_orders) >= pr.max_orders:
                break
        else:
            log.debug("[%s] Cycle %d: %d attempts (%.0fms)", pr.profile_id, scan, len(fire), (time.monotonic() - t0) * 1000)
        if oneshot:
            break
        time.sleep(max(0.3, pr.poll - (time.monotonic() - t0)))

    ctx = release_context or {}
    if ctx.get("from_discovery") and first_order_mono is not None and not pr.release_report_sent:
        det_m = detection_mono
        wall_det = ctx.get("detection_wall_dt")
        placed_this_fire = len(held_orders) - orders_before_fire
        best_line = ""
        if held_orders:
            last = held_orders[-1]
            best_line = f"`{last.get('seats', '')}` score `{last.get('score', '')}`"
        first_hold_line = ""
        fire_slice = held_orders[orders_before_fire : orders_before_fire + placed_this_fire]
        if fire_slice:
            h0 = fire_slice[0]
            first_hold_line = f"`{h0.get('seats', '')}` score `{h0.get('score', '')}`"
        desc_lines = [
            f"**Movie:** {pr.movie_name or pr.movie}",
            f"**Showtimes in batch:** `{len(showtimes)}`",
            f"**Orders placed this fire:** `{placed_this_fire}` (session total: `{_session_order_count(pr.profile_id)}`)",
        ]
        if first_hold_line:
            desc_lines.append(f"**First hold (best batch attempt):** {first_hold_line}")
        if wall_det:
            desc_lines.append(
                f"**First detection (wall):** `{_fmt_dt_et_stamp(wall_det, cfg.tz)}` **({wall_det.strftime('%A')})**"
            )
        if det_m is not None:
            desc_lines.append(f"**Detection → first hold:** `{((first_order_mono - det_m) * 1000):.0f} ms`")
        if _session_start_mono > 0:
            desc_lines.append(
                f"**Bot uptime at first hold:** `{_format_duration(first_order_mono - _session_start_mono)}`"
            )
        if best_line:
            desc_lines.append(f"**Last hold:** {best_line}")
        desc_lines.append(
            f"**Tuning snapshot:** `poll={pr.poll}s` · `discovery_interval={pr.discovery_interval}s` · `workers={pr.workers}`"
        )
        _send_discord(
            cfg,
            {
                "embeds": [
                    _base_embed(
                        f"[RELEASE REPORT] {pr.movie_name or pr.movie}",
                        0xE67E22,
                        description="\n".join(desc_lines),
                        url=_amc_movie_link(pr),
                        pr=pr,
                    )
                ]
            },
        )
        pr.release_report_sent = True

    return scan


def snipe_scan(pr: ProfileRuntime, client: AMCClient, cfg: Cfg, showtimes: List[Showtime], state: Dict, held_orders: List[Dict]):
    _prune_stale_holds(held_orders, cfg.tz)
    ts = _ts()
    now = _now(cfg.tz)
    cooldown_cutoff = now - timedelta(minutes=pr.alert_cooldown_minutes)
    state.setdefault("st", {})
    first = not state.get("init") and pr.suppress_first
    alerts: List[Dict] = []
    startup: List[Dict] = []

    def work(st):
        try:
            return st, *check_seats(pr, client, st), None
        except Exception as e:
            return st, [], [], e

    with concurrent.futures.ThreadPoolExecutor(max_workers=pr.workers) as ex:
        for st, good, groups, err in ex.map(work, showtimes):
            if err:
                log.debug("[%s] Seat check SID %d: %s", pr.profile_id, st.sid, err)
                continue
            if good or groups:
                bg = f"{groups[0].names} ({groups[0].score})" if groups else "—"
                log.info("  %s %5s%s  %dg %dgrp  %s", st.date, st.time, st.ampm, len(good), len(groups), bg)
            k = str(st.sid)
            prev = state["st"].get(k, {})
            pg = set(prev.get("gids", []))

            prev_alerted_raw = prev.get("alerted_gids", {})
            if isinstance(prev_alerted_raw, list):
                prev_alerted_raw = {}
            active_alerted = {
                gid for gid, ts_str in prev_alerted_raw.items()
                if _hold_created_dt(ts_str, cfg.tz) and _hold_created_dt(ts_str, cfg.tz) > cooldown_cutoff
            }

            new_g = [g for g in groups if g.id not in pg and g.id not in active_alerted]

            updated_alerted = {
                gid: t for gid, t in prev_alerted_raw.items()
                if _hold_created_dt(t, cfg.tz) and _hold_created_dt(t, cfg.tz) > cooldown_cutoff
            }
            state["st"][k] = {"gids": [g.id for g in groups], "gc": len(good), "at": ts, "alerted_gids": updated_alerted}

            if first:
                elite = [g for g in groups if g.score >= 80]
                if elite:
                    for g in elite:
                        updated_alerted[g.id] = ts
                    state["st"][k]["alerted_gids"] = updated_alerted
                    r, reason = _try_reserve_with_reason(pr, client, cfg, st, elite[0], held_orders)
                    hf = reason if pr.auto_reserve and not r else ""
                    startup.append({"st": st, "groups": elite, "best": elite[0], "reserved": r, "tag": "STARTUP", "hold_failed_reason": hf})
                continue
            if new_g:
                for g in new_g:
                    updated_alerted[g.id] = ts
                state["st"][k]["alerted_gids"] = updated_alerted
                r, reason = _try_reserve_with_reason(pr, client, cfg, st, new_g[0], held_orders)
                hf = reason if pr.auto_reserve and not r else ""
                alerts.append({"st": st, "groups": new_g, "best": groups[0] if groups else None, "reserved": r, "tag": "SNIPE", "hold_failed_reason": hf})
    n_startup = len(startup)
    if startup:
        log.info("[%s] Startup: %d ELITE row(s)", pr.profile_id, n_startup)
        alerts = startup + alerts
    state["init"] = True
    log.info(
        "[%s] Snipe pass: %d showtime(s) scanned → %d alert row(s) (startup_elite=%d)",
        pr.profile_id,
        len(showtimes),
        len(alerts),
        n_startup,
    )
    return alerts


def _try_reserve(pr: ProfileRuntime, client: AMCClient, cfg: Cfg, st: Showtime, group: SeatGroup, held_orders: List[Dict]) -> Optional[Dict]:
    r, _ = _try_reserve_with_reason(pr, client, cfg, st, group, held_orders)
    return r


def _try_reserve_with_reason(pr: ProfileRuntime, client: AMCClient, cfg: Cfg, st: Showtime, group: SeatGroup, held_orders: List[Dict]) -> Tuple[Optional[Dict], str]:
    _prune_stale_holds(held_orders, cfg.tz)
    if not pr.auto_reserve:
        return None, "Auto-reserve is off"
    if len(held_orders) >= pr.max_orders:
        return None, f"Max orders reached ({pr.max_orders})"
    try:
        order = client.create_order(st.sid, group.coords)
        token = order.get("token")
        if token:
            entry = {
                "token": token, "sid": st.sid, "seats": group.names, "score": group.score,
                "date": st.date, "time": f"{st.time} {st.ampm}",
                "movie": pr.movie_name or pr.movie_slug,
                "url": f"{BASE}/orders/{token}/food-and-drink",
                "total": order.get("displayTotal", ""), "created": _now(cfg.tz).isoformat(),
            }
            held_orders.append(entry)
            _record_order_placed(pr.profile_id)
            log.info("  RESERVED %s for %s → %s", group.names, st.label, entry["url"])
            return entry, ""
        log.warning("  Reserve %s sid=%s: API response had no token", group.names, st.sid)
        return None, "API returned no token"
    except Exception as e:
        log.warning("  Reserve %s: %s", group.names, e)
        return None, str(e)


class DiscoveryEngine:
    _DORMANT_META_INTERVAL = 4 * 3600

    def __init__(self, pr: ProfileRuntime, client: AMCClient, cfg: Cfg, state: Dict, held_orders: List[Dict]):
        self.pr = pr
        self.client = client
        self.cfg = cfg
        self.state = state
        self.held_orders = held_orders
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.known: Set[int] = set()
        self._cache: List[Showtime] = []
        self._showtimes_ever_found = False
        self._prev_phase: Optional[str] = None
        self._release_mode_fired = False
        self._discovery_cycles = 0
        self._engine_started_mono = time.monotonic()
        self._last_meta_check_mono: float = 0.0

    def seed(self, sts: List[Showtime]):
        with self._lock:
            self.known = {s.sid for s in sts}
            self._cache = list(sts)
            if sts:
                self._showtimes_ever_found = True

    def get_cached(self) -> List[Showtime]:
        with self._lock:
            now = _now(self.cfg.tz)
            grace = timedelta(minutes=self.pr.post_showtime_minutes)
            return [st for st in self._cache if _dt(st, self.cfg.tz) + grace > now]

    def _current_phase(self) -> str:
        if self._showtimes_ever_found:
            return "active"
        return _profile_phase(self.pr, self.cfg.tz)

    def _notify_phase_change(self, old_phase: str, new_phase: str):
        pr = self.pr
        rd = _parse_release_date(pr.release_date)
        days_away = (rd - _now(self.cfg.tz).date()).days if rd else 0
        color = {"dormant": 0x95A5A6, "watch": 0xF39C12, "active": 0x2ECC71}.get(new_phase, 0x3498DB)
        movie_label = pr.movie_name or pr.movie

        if new_phase == "active":
            title = f"⚡  ACTIVE — {movie_label}"
            color = 0x2ECC71
            desc = f"**{movie_label}** is now in **Active** monitoring.\n"
            desc += f"`{old_phase.title()}` → **`Active`**\n\n"
            if rd:
                desc += f"📅 **Release:** `{_fmt_date(str(rd))}`"
                if days_away > 0:
                    desc += f" — **{days_away} days away**"
                elif days_away == 0:
                    desc += " — **TODAY**"
                else:
                    desc += f" — **{abs(days_away)} days ago**"
                desc += "\n"
            desc += "🔥 Full-speed seat scanning, showtime discovery, and auto-reserve are **live**.\n"
            desc += "New showtimes will be detected and seats acquired immediately."
        elif new_phase == "watch":
            title = f"👁️  Watch — {movie_label}"
            desc = f"**{movie_label}** entered the **Watch** phase.\n"
            desc += f"`{old_phase.title()}` → **`Watch`**\n\n"
            if rd:
                desc += f"📅 **Release:** `{_fmt_date(str(rd))}` — **{days_away} days away**\n"
            desc += "Scanning at full discovery cadence for presale / on-sale ticket drops."
        else:
            title = f"💤  Dormant — {movie_label}"
            desc = f"**{movie_label}** entered **Dormant** phase.\n"
            desc += f"`{old_phase.title()}` → **`Dormant`**\n\n"
            if rd:
                desc += f"📅 **Release:** `{_fmt_date(str(rd))}` — **{days_away} days away**\n"
            desc += "Metadata-only checks every 4h until presale window or release approach."

        embed = _base_embed(title, color, description=desc, url=_amc_movie_link(pr), pr=pr)
        embed["fields"] = _movie_info_fields(pr)
        _send_discord(self.cfg, {"embeds": [embed]})

    def _refresh_metadata(self):
        try:
            m = self.client.resolve_movie(self.pr.movie)
            if m:
                new_rd = m.get("release_date", "")
                if new_rd and new_rd != self.pr.release_date:
                    log.info("[%s] Release date updated: %s → %s", self.pr.profile_id, self.pr.release_date[:10] if self.pr.release_date else "?", new_rd[:10])
                    self.pr.release_date = new_rd
                    self.pr.ticket_avail_date = m.get("ticket_avail_date", "")
                rt = int(m.get("run_time") or 0)
                if rt:
                    self.pr.run_time_minutes = rt
        except Exception as e:
            log.debug("[%s] Metadata refresh: %s", self.pr.profile_id, e)

    def _early_drop_dates(self) -> List[str]:
        rd = _parse_release_date(self.pr.release_date)
        if not rd:
            return []
        today = _now(self.cfg.tz).date()
        dates: List[str] = []
        for delta in range(-1, 4):
            d = rd + timedelta(days=delta)
            if d >= today:
                dates.append(d.strftime("%Y-%m-%d"))
        return dates

    def _run_discovery_cycle(self, *, _dates_override: Optional[List[str]] = None) -> bool:
        had_any_before = self._showtimes_ever_found
        wall_before = _now(self.cfg.tz)
        found = discover_showtimes(self.pr, self.client, self.cfg, _dates_override=_dates_override)
        curr = {st.sid for st in found}
        with self._lock:
            new = curr - self.known
            self.known = curr
            self._cache = list(found)
        self._discovery_cycles += 1

        is_early_drop = _dates_override is not None
        phase_label = "dormant_early_drop" if is_early_drop else ("watch" if not had_any_before else "active")
        log.info(
            "RELEASE_FLOW [%s] DISCOVERY_CYCLE #%d phase=%s had_showtimes_before=%s found=%d new_ids=%d wall=%s%s",
            self.pr.profile_id,
            self._discovery_cycles,
            phase_label,
            had_any_before,
            len(found),
            len(new),
            wall_before.strftime("%m-%d-%Y %H:%M:%S %Z"),
            " [EARLY DROP CHECK]" if is_early_drop else "",
        )

        if (
            self._discovery_cycles % DISCOVERY_HEARTBEAT_INTERVAL == 0
            and not found
            and not had_any_before
        ):
            elapsed = time.monotonic() - self._engine_started_mono
            log.info(
                "HEARTBEAT [%s] %d discovery cycles, 0 showtimes found, running %s, phase=%s%s",
                self.pr.profile_id,
                self._discovery_cycles,
                _format_duration(elapsed),
                phase_label,
                " [early_drop_check scanning release window]" if is_early_drop else "",
            )
        elif (
            self._discovery_cycles % DISCOVERY_HEARTBEAT_INTERVAL == 0
            and had_any_before
            and len(new) == 0
        ):
            elapsed = time.monotonic() - self._engine_started_mono
            log.info(
                "HEARTBEAT [%s] %d discovery cycles, 0 new showtimes (have %d cached), running %s",
                self.pr.profile_id,
                self._discovery_cycles,
                len(found),
                _format_duration(elapsed),
            )

        is_first_batch = bool(found and not had_any_before)
        if is_first_batch:
            self._showtimes_ever_found = True
            first_detect_mono = time.monotonic()
            det_wall = _now(self.cfg.tz)
            dmin = dmax = found[0].date
            for s in found:
                if s.date < dmin:
                    dmin = s.date
                if s.date > dmax:
                    dmax = s.date
            theatres_hit = len({s.theatre_slug for s in found if s.theatre_slug}) or len(self.pr.theatres)
            since_boot = ""
            if _session_start_mono > 0:
                since_boot = f" since_boot={_format_duration(time.monotonic() - _session_start_mono)}"
            since_presale = ""
            ps = _parse_release_date(self.pr.ticket_presale_start)
            if ps:
                try:
                    since_presale = f" since_presale_start={(det_wall.date() - ps).days}d"
                except Exception:
                    pass
            movie_lbl = self.pr.movie_name or self.pr.movie
            theatres_str = ", ".join(sorted({s.theatre_name or s.theatre_slug for s in found if (s.theatre_name or s.theatre_slug)}))[:200]
            source_tag = "EARLY_DROP" if is_early_drop else "FIRST_SHOWTIMES_DETECTED"
            log.info(
                "RELEASE_FLOW [%s] %s movie=%r at %s (%s) — %d showtimes, %d theatres (distinct), dates %s..%s theatres_sample=%r%s%s sample=[%s]",
                self.pr.profile_id,
                source_tag,
                movie_lbl,
                _fmt_dt_et_stamp(det_wall, self.cfg.tz),
                det_wall.strftime("%A"),
                len(found),
                theatres_hit,
                dmin,
                dmax,
                theatres_str or "(see profile)",
                since_boot,
                since_presale,
                _summarize_showtimes_for_log(found, 6),
            )

            if new:
                new_sts = [s for s in found if s.sid in new]
                first_release = (self.pr.original_mode == "release" and not self._release_mode_fired)
                if first_release:
                    release_wall = _now(self.cfg.tz)
                    log.info(
                        "RELEASE_FLOW [%s] FIRST_DROP_RELEASE_BATCH (full release_mode on %d showtimes)%s",
                        self.pr.profile_id,
                        len(found),
                        " [triggered by early_drop_check]" if is_early_drop else "",
                    )
                    t_rel0 = time.monotonic()
                    cycles = release_mode(
                        self.pr,
                        self.client,
                        self.cfg,
                        found,
                        self.held_orders,
                        oneshot=False,
                        alert_tag="RELEASE DAY",
                        release_context={
                            "from_discovery": True,
                            "detection_mono": first_detect_mono,
                            "detection_wall_dt": release_wall,
                        },
                    )
                    self._release_mode_fired = True
                    log.info(
                        "RELEASE_FLOW [%s] release_mode finished cycles=%d elapsed=%s held=%d/%d",
                        self.pr.profile_id,
                        cycles,
                        _format_duration(time.monotonic() - t_rel0),
                        len(self.held_orders),
                        self.pr.max_orders,
                    )
                    if cycles == 0:
                        log.warning(
                            "[%s] Release batch had no scorable seats — falling back to per-showtime aggressive reserve",
                            self.pr.profile_id,
                        )
                        self._acquire_new_showtimes(new_sts, alert_tag="RELEASE DAY")
                    if len(self.held_orders) < self.pr.max_orders:
                        rel_elapsed = time.monotonic() - t_rel0
                        log.info(
                            "RELEASE_FLOW [%s] RELEASE_TO_SNIPE transition orders=%d/%d release_elapsed=%s",
                            self.pr.profile_id,
                            len(self.held_orders),
                            self.pr.max_orders,
                            _format_duration(rel_elapsed),
                        )
                        _discord_mode_release_to_snipe(
                            self.cfg,
                            self.pr,
                            orders_held=len(self.held_orders),
                            release_elapsed_sec=rel_elapsed,
                            source="discovery_first_drop" + ("_early_drop" if is_early_drop else ""),
                            showtimes_in_release=len(found),
                        )
                        self.pr.mode = "snipe"
                else:
                    self._acquire_new_showtimes(new_sts, alert_tag="NEW SHOWTIME")
        elif new:
            new_sts = [s for s in found if s.sid in new]
            log.info("[%s] %d NEW showtime(s) detected (incremental)", self.pr.profile_id, len(new_sts))
            self._acquire_new_showtimes(new_sts, alert_tag="NEW SHOWTIME")

        return is_first_batch

    def run(self):
        while not self._stop.is_set():
            phase = self._current_phase()

            if self._prev_phase and phase != self._prev_phase:
                log.info("[%s] Phase transition: %s → %s", self.pr.profile_id, self._prev_phase, phase)
                self._notify_phase_change(self._prev_phase, phase)
            self._prev_phase = phase

            if phase == "dormant":
                if self.pr.early_drop_check:
                    self._stop.wait(self.pr.discovery_interval)
                    if self._stop.is_set():
                        break
                    elapsed_since_meta = time.monotonic() - self._last_meta_check_mono
                    if elapsed_since_meta >= self._DORMANT_META_INTERVAL or self._last_meta_check_mono == 0:
                        self._refresh_metadata()
                        self._last_meta_check_mono = time.monotonic()
                    early_dates = self._early_drop_dates()
                    if early_dates:
                        try:
                            self._run_discovery_cycle(_dates_override=early_dates)
                        except Exception as e:
                            log.error("[%s] Early-drop check error: %s\n%s", self.pr.profile_id, e, traceback.format_exc())
                    else:
                        log.debug("[%s] early_drop_check: no release date set, skipping", self.pr.profile_id)
                else:
                    self._stop.wait(self._DORMANT_META_INTERVAL)
                    if self._stop.is_set():
                        break
                    self._refresh_metadata()
                continue

            wait = self.pr.discovery_interval
            self._stop.wait(wait)
            if self._stop.is_set():
                break

            try:
                self._run_discovery_cycle()
            except Exception as e:
                log.error("[%s] Discovery error: %s\n%s", self.pr.profile_id, e, traceback.format_exc())

    def _acquire_new_showtimes(self, new_sts: List[Showtime], *, alert_tag: str = "NEW SHOWTIME"):
        _prune_stale_holds(self.held_orders, self.cfg.tz)
        t0 = time.monotonic()
        for st in new_sts:
            if len(self.held_orders) >= self.pr.max_orders:
                log.info("[%s] Max orders (%d) reached — skipping remaining new showtimes", self.pr.profile_id, self.pr.max_orders)
                break
            try:
                _, groups = _check_seats_aggressive(self.pr, self.client, st)
                if not groups:
                    log.info("[%s] New SID %d (%s %s%s): no groups of %d", self.pr.profile_id, st.sid, st.date, st.time, st.ampm, self.pr.quantity)
                    continue
                best = groups[0]
                log.info("[%s] New SID %d: best=%s (%.1f) — reserving aggressively", self.pr.profile_id, st.sid, best.names, best.score)
                r, fail_reason = _try_reserve_with_reason(self.pr, self.client, self.cfg, st, best, self.held_orders)
                ms = (time.monotonic() - t0) * 1000
                log.info(
                    "RELEASE_FLOW [%s] NEW_SHOWTIME_RESERVE sid=%d tag=%s ok=%s latency_ms=%.0f",
                    self.pr.profile_id,
                    st.sid,
                    alert_tag,
                    bool(r and r.get("token")),
                    ms,
                )
                _send_alert(self.cfg, self.pr, st, best, r, alert_tag, hold_failed_reason=fail_reason)
            except Exception as e:
                log.warning("[%s] New st %d acquisition: %s", self.pr.profile_id, st.sid, e)

    def stop(self):
        self._stop.set()


_discord_post_lock = threading.Lock()
_discord_last_post_mono: float = 0.0
DISCORD_MIN_POST_INTERVAL_SEC = 0.65


def _footer() -> Dict:
    return {"text": f"AMC+ v{VERSION} · by habib · {_footer_stamp()}", "icon_url": FOOTER_ICON}


def _author() -> Dict:
    return {"name": PRODUCT, "icon_url": AUTHOR_ICON, "url": AUTHOR_URL}


def _base_embed(title: str, color: int, *, description: str = "", url: str = "", pr: Optional[ProfileRuntime] = None) -> Dict:
    embed: Dict[str, Any] = {
        "title": title[:256] if title else title,
        "color": color,
        "author": _author(),
        "footer": _footer(),
        "timestamp": datetime.now(tz=ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if url:
        embed["url"] = url
    if description:
        embed["description"] = description
    if pr:
        poster = _get_poster(pr)
        if poster:
            embed["thumbnail"] = {"url": poster}
    return embed


def _send_discord(cfg: Cfg, payload: Dict):
    if not cfg.discord.webhook_url:
        return
    threading.Thread(target=_send_discord_sync, args=(cfg, payload), daemon=True).start()


def _send_discord_sync(cfg: Cfg, payload: Dict):
    global _discord_last_post_mono
    if not cfg.discord.webhook_url:
        return
    payload["username"] = DISCORD_USERNAME
    payload["avatar_url"] = DISCORD_AVATAR

    for emb in payload.get("embeds") or []:
        title = (emb.get("title") or "").strip()
        desc = (emb.get("description") or "").strip()
        if not title and not desc:
            emb["title"] = "AMC+"
        elif title and len(title) > 256:
            emb["title"] = title[:253] + "…"
        if desc and len(desc) > 4096:
            emb["description"] = desc[:4093] + "…"
        for fld in emb.get("fields") or []:
            v = fld.get("value")
            if v is None or (isinstance(v, str) and not v.strip()):
                fld["value"] = "`—`"
            elif isinstance(v, str) and len(v) > 1024:
                fld["value"] = v[:1021] + "…"

    attempt = 0
    backoff = 0.0
    while True:
        attempt += 1
        try:
            if backoff > 0:
                time.sleep(backoff)
                backoff = 0.0
            with _discord_post_lock:
                gap = DISCORD_MIN_POST_INTERVAL_SEC - (
                    time.monotonic() - _discord_last_post_mono
                )
                if gap > 0:
                    time.sleep(gap)
                r = requests.post(cfg.discord.webhook_url, json=payload, timeout=30)
                _discord_last_post_mono = time.monotonic()

            if r.status_code in (200, 204):
                if attempt > 1:
                    log.info("Discord delivered after %d attempt(s)", attempt)
                return

            if r.status_code == 429:
                try:
                    body = r.json()
                except Exception:
                    body = {}
                raw = body.get("retry_after", 5)
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = 5.0
                if retry_after <= 0:
                    retry_after = 5.0
                is_global = body.get("global", False)
                if attempt == 1 or attempt % 5 == 0:
                    log.warning(
                        "Discord 429 (try %d, global=%s): waiting %.1fs — will retry until delivered bucket=%s",
                        attempt,
                        is_global,
                        retry_after,
                        body.get("bucket", "?"),
                    )
                backoff = retry_after + 0.35
                continue

            if r.status_code >= 500:
                backoff = min(120.0, 5.0 * min(attempt, 24))
                log.warning(
                    "Discord %d (try %d): retry in %.0fs — %s",
                    r.status_code,
                    attempt,
                    backoff,
                    (r.text or "")[:200],
                )
                continue

            if r.status_code in (400, 401, 403, 404):
                log.error(
                    "Discord HTTP %s (will not retry): %s",
                    r.status_code,
                    (r.text or "")[:800],
                )
                return

            backoff = min(60.0, 10.0 + attempt)
            log.warning("Discord HTTP %s: retry in %.0fs — %s", r.status_code, backoff, (r.text or "")[:300])

        except requests.exceptions.Timeout:
            backoff = min(90.0, 5.0 * min(attempt, 18))
            log.warning("Discord timeout (try %d): retry in %.0fs", attempt, backoff)
        except requests.exceptions.ConnectionError as e:
            backoff = min(120.0, 8.0 * min(attempt, 15))
            log.warning("Discord connection error (try %d): retry in %.0fs — %s", attempt, backoff, e)
        except Exception as e:
            backoff = min(60.0, 15.0)
            log.error("Discord send error (try %d): %s — retry in %.0fs", attempt, e, backoff)


def _movie_info_fields(pr: ProfileRuntime) -> List[Dict]:
    fields: List[Dict] = []
    rd = _parse_release_date(pr.release_date)
    if rd:
        fields.append({"name": "Release Date", "value": f"`{_fmt_date(str(rd))}`", "inline": True})
    rt = _format_runtime_minutes(pr.run_time_minutes)
    if rt:
        fields.append({"name": "Runtime", "value": f"`{rt}`", "inline": True})
    if pr.format:
        fields.append({"name": "Format", "value": f"`{pr.format}`", "inline": True})
    if pr.theatre_name:
        fields.append({"name": "Theatre", "value": f"`{pr.theatre_name}`", "inline": True})
    if pr.theatres:
        addr0 = pr.theatre_addresses.get(pr.theatres[0], "")
        if addr0:
            fields.append(
                {
                    "name": "Theatre address",
                    "value": addr0[:1024],
                    "inline": False,
                }
            )
    fields.append({"name": "Mode", "value": f"`{pr.mode.title()}`", "inline": True})
    fields.append({"name": "Auto-Reserve", "value": f"`{'On' if pr.auto_reserve else 'Off'}`", "inline": True})
    return fields


def _send_alert(cfg: Cfg, pr: ProfileRuntime, st: Optional[Showtime], group: Optional[SeatGroup], reserved: Optional[Dict], tag: str = "SNIPE", *, hold_failed_reason: str = ""):
    if not cfg.discord.webhook_url:
        return
    bs = group.score if group else (reserved.get("score", 0) if reserved else 0)
    if "RELEASE" in tag:
        color = 0xFF0000
    elif "NEW" in tag:
        color = 0x9B59B6
    else:
        color = 0xFFD700 if bs >= 80 else 0x1DB954 if bs >= 70 else 0x3498DB

    showtime_label = ""
    if st:
        showtime_label = f" · {_fmt_showtime_display(st.date, st.time, st.ampm)}"
    title = f"[{tag}] {pr.movie_name or pr.movie_slug}{showtime_label}"

    hold_expires_line = f"> Hold expires at **{_fmt_hold_expires_et(cfg)}**. Open in logged-in browser."
    atc_url = ""
    if group and st:
        atc_url = f"{st.ticket_url}{quote(group.query, safe='')}"

    embed_url = ""
    desc = ""
    if reserved:
        embed_url = reserved["url"]
        desc = f"**Checkout:** [{reserved['seats']}]({reserved['url']})\n{hold_expires_line}"
    elif pr.auto_reserve and group and st:
        color = 0xFF9900
        fail_reason_display = hold_failed_reason or "Unknown — check logs"
        embed_url = atc_url
        desc = "🚫 **AUTO-RESERVE FAILED** — seats were found but the hold **could not** be placed.\n\n"
        desc += f"**Reason:** `{fail_reason_display}`\n\n"
        desc += f"**[➡️ Open seat selection (ATC)]({atc_url})** — try to reserve manually before they're gone.\n"
        desc += f"{hold_expires_line}"
    elif not pr.auto_reserve and group and st:
        embed_url = atc_url
        desc = f"**Select Seats:** [{group.names}]({atc_url})"
    elif group and st:
        embed_url = st.url or ""
    else:
        embed_url = st.url if st else ""

    fields: List[Dict] = []
    if st:
        fields.append(
            {
                "name": "Showtime",
                "value": f"`{_fmt_showtime_display(st.date, st.time, st.ampm)}`",
                "inline": True,
            }
        )
        if st.theatre_name:
            fields.append({"name": "Theatre", "value": f"`{st.theatre_name}`", "inline": True})
        if st.theatre_slug:
            addr = pr.theatre_addresses.get(st.theatre_slug, "")
            if addr:
                fields.append(
                    {"name": "Theatre address", "value": addr[:1024], "inline": False}
                )
        if st.auditorium:
            fields.append({"name": "Auditorium", "value": f"`#{st.auditorium}`", "inline": True})
    fields.append({"name": "Format", "value": f"`{st.format_label if st else pr.premium_label}`", "inline": True})
    if st:
        if st.discount_eligible is False or st.discount_excluded:
            fields.append({"name": "Discount", "value": "`Excluded`", "inline": True})
        elif st.discount_eligible:
            fields.append({"name": "Discount", "value": "`50% Off`", "inline": True})
        if st.price_label:
            fields.append({"name": "Price (Per Ticket)", "value": f"`{st.price_label}`", "inline": True})
    rt = _format_runtime_minutes(pr.run_time_minutes)
    if rt:
        fields.append({"name": "Runtime", "value": f"`{rt}`", "inline": True})
    fields.append({"name": "Mode", "value": f"`{pr.mode.title()}`", "inline": True})
    fields.append({"name": "Auto-Reserve", "value": f"`{'On' if pr.auto_reserve else 'Off'}`", "inline": True})
    if pr.auto_reserve:
        if reserved:
            fields.append({"name": "Checkout Hold", "value": f"`Active — {_fmt_hold_expires_et(cfg)}`", "inline": True})
        elif group and st:
            fail_short = (hold_failed_reason or "Unknown")[:80]
            fields.append({"name": "Checkout Hold", "value": f"`FAILED`", "inline": True})
            fields.append({"name": "Failure Reason", "value": f"`{fail_short}`", "inline": True})
    elif group and st:
        fields.append({"name": "Checkout Hold", "value": "`Off (manual ATC)`", "inline": True})

    if reserved:
        fields.append({"name": "Seats", "value": f"`{reserved['seats']}`", "inline": True})
        fields.append({"name": "Score", "value": f"`{reserved['score']}`", "inline": True})
        fields.append({"name": "Tier", "value": f"`{tier_label(reserved['score'])}`", "inline": True})
        if reserved.get("total"):
            fields.append({"name": "Total", "value": f"`{reserved['total']}`", "inline": True})
    elif group:
        fields.append({"name": "Seats", "value": f"`{group.names}`", "inline": True})
        fields.append({"name": "Score", "value": f"`{group.score}`", "inline": True})
        fields.append({"name": "Tier", "value": f"`{group.tier}`", "inline": True})

    embed = _base_embed(title, color, description=desc, url=embed_url, pr=pr)
    embed["fields"] = fields
    _send_discord(cfg, {"embeds": [embed]})


def _notify_batch(cfg: Cfg, pr: ProfileRuntime, alerts: List[Dict]):
    cap = 15
    n = min(len(alerts), cap)
    extra = f" (truncated, {len(alerts)} total)" if len(alerts) > cap else ""
    log.info("[%s] Discord ticket batch: posting %d embed(s)%s", pr.profile_id, n, extra)
    for a in alerts[:cap]:
        _send_alert(
            cfg, pr, a.get("st"), a.get("best"), a.get("reserved"), a.get("tag", "SNIPE"),
            hold_failed_reason=a.get("hold_failed_reason") or "",
        )


def _profile_startup_embed(pr: ProfileRuntime, n_show: int, cfg: Cfg) -> Dict[str, Any]:
    phase = _profile_phase(pr, cfg.tz) if pr.movie_id else "waiting"
    phase_disp = phase.title() if isinstance(phase, str) else str(phase)
    rd = _parse_release_date(pr.release_date)
    rt = _format_runtime_minutes(pr.run_time_minutes)
    presale = (pr.ticket_presale_start or "").strip()
    if pr.target_dates:
        td = ", ".join(pr.target_dates[:4])
        if len(pr.target_dates) > 4:
            td += f" (+{len(pr.target_dates) - 4})"
    else:
        td = "—"
    if len(pr.theatres) <= 2:
        theatre_disp = pr.theatre_name or ", ".join(pr.theatres) if pr.theatres else "—"
    else:
        theatre_disp = f"{pr.theatre_name or pr.theatres[0]} (+{len(pr.theatres) - 1} more)"
    min_s = str(pr.min_score) if pr.min_score else "tier"
    fields: List[Dict[str, Any]] = [
        {"name": "Showtimes", "value": f"`{n_show}`", "inline": True},
        {"name": "Phase", "value": f"`{phase_disp}`", "inline": True},
        {"name": "Mode", "value": f"`{pr.mode.title()}` / orig `{pr.original_mode.title()}`", "inline": True},
        {"name": "Format", "value": f"`{pr.format or 'All'}`", "inline": True},
        {"name": "Theatre(s)", "value": theatre_disp[:1024], "inline": False},
        {
            "name": "Qty / Tier / Min score",
            "value": f"`{pr.quantity}` / `{pr.tier}` / `{min_s}`",
            "inline": True,
        },
        {"name": "Poll / Discovery", "value": f"`{pr.poll}s` / `{pr.discovery_interval}s`", "inline": True},
        {"name": "Presale start", "value": f"`{presale or '—'}`", "inline": True},
        {"name": "Target dates", "value": (f"`{td}`")[:1024], "inline": False},
        {"name": "Release", "value": f"`{_fmt_date(str(rd))}`" if rd else "`—`", "inline": True},
        {"name": "Runtime", "value": f"`{rt or '—'}`", "inline": True},
        {
            "name": "Auto-reserve / Discount",
            "value": f"`{'On' if pr.auto_reserve else 'Off'}` / `{pr.discount or 'off'}`",
            "inline": True,
        },
    ]
    embed = _base_embed(pr.movie_name or pr.movie, 0x3498DB, url=_amc_movie_link(pr), pr=pr)
    embed["fields"] = fields
    return embed


def _discord_startup(cfg: Cfg, profiles: List[ProfileRuntime], showtime_counts: Dict[str, int]):
    if not cfg.discord.webhook_url:
        return
    n_profiles = len(profiles)
    n_active = sum(
        1 for pr in profiles if pr.movie_id and _profile_phase(pr, cfg.tz) == "active"
    )
    n_watch = sum(
        1 for pr in profiles if pr.movie_id and _profile_phase(pr, cfg.tz) == "watch"
    )
    n_dormant = sum(
        1 for pr in profiles if pr.movie_id and _profile_phase(pr, cfg.tz) == "dormant"
    )
    n_waiting = sum(1 for pr in profiles if not pr.movie_id)

    n_overflow_pages = max(0, (len(profiles) - 9 + 9) // 10)
    total_pages = 1 + n_overflow_pages
    header = _base_embed(
        f"🟢  {PRODUCT} Started — {n_profiles} Profiles",
        0x2ECC71,
        description=f"Per-profile cards below — `{n_profiles}` profiles across `{total_pages}` message(s).",
    )
    header["fields"] = [
        {"name": "Active", "value": f"`{n_active}`", "inline": True},
        {"name": "Watch", "value": f"`{n_watch}`", "inline": True},
        {"name": "Dormant", "value": f"`{n_dormant}`", "inline": True},
        {"name": "Waiting (not on AMC)", "value": f"`{n_waiting}`", "inline": True},
    ]
    profile_embeds = [_profile_startup_embed(pr, showtime_counts.get(pr.profile_id, 0), cfg) for pr in profiles]
    first_payload = [header] + profile_embeds[:9]
    _send_discord(cfg, {"embeds": first_payload})
    for page, i in enumerate(range(9, len(profile_embeds), 10), start=2):
        cont = _base_embed(
            f"🟢  {PRODUCT} Started — Profiles (cont. {page}/{total_pages})",
            0x2ECC71,
        )
        _send_discord(cfg, {"embeds": [cont] + profile_embeds[i : i + 9]})


def _discord_shutdown(
    cfg: Cfg,
    scan_count: int,
    all_orders: List[Dict],
    *,
    runners: Optional[List[ProfileRunner]] = None,
):
    stop_dt = _now(cfg.tz)
    stopped_str = _fmt_dt_et_stamp(stop_dt, cfg.tz)
    started_str = _fmt_dt_et_stamp(_bot_start_time, cfg.tz) if _bot_start_time else "—"
    runtime_s = (
        (stop_dt - _bot_start_time).total_seconds()
        if _bot_start_time
        else 0.0
    )
    runtime_str = _format_duration(runtime_s) if _bot_start_time else "—"
    total_placed = _total_session_orders()
    total_seats_held = sum(_seat_count_from_hold_entry(o) for o in all_orders)

    desc_lines: List[str] = []
    if runners:
        desc_lines.append("**━━━ Per-profile ━━━**")
        for r in runners:
            placed = _session_order_count(r.pr.profile_id)
            desc_lines.append(
                f"**{r.pr.movie_name or r.pr.movie}** — scans `{r.scan_count}` · "
                f"placed `{placed}` · held `{len(r.held_orders)}`"
            )

    if all_orders:
        if desc_lines:
            desc_lines.append("")
        desc_lines.append(f"**━━━ Holds remaining ({len(all_orders)}) ━━━**")
        for o in all_orders:
            desc_lines.append(f"🎟️ `{o['seats']}` — **{o.get('movie', '')}** — [{_fmt_date(o['date'])} {o['time']}]({o['url']})")

    desc = "\n".join(desc_lines)
    if len(desc) > 4000:
        desc = desc[:3997] + "…"

    embed = _base_embed(f"🔴  {PRODUCT} Stopped", 0xE74C3C, description=desc)
    embed["fields"] = [
        {"name": "Started At", "value": f"`{started_str}`", "inline": True},
        {"name": "Stopped At", "value": f"`{stopped_str}`", "inline": True},
        {"name": "Runtime", "value": f"`{runtime_str}`", "inline": True},
        {"name": "Global scan cycles", "value": f"`{scan_count:,}`", "inline": True},
        {"name": "Total orders placed", "value": f"`{total_placed}`", "inline": True},
        {"name": "Orders / seats held now", "value": f"`{len(all_orders)}` / `{total_seats_held}`", "inline": True},
    ]
    _send_discord_sync(cfg, {"embeds": [embed]})


def _load(p: str) -> Dict:
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    if not os.path.exists(p):
        log.info("No state file at %r — starting fresh", p)
        return {"init": False, "profiles": {}}
    try:
        with open(p) as f:
            data = json.load(f)
        if "profiles" not in data:
            data["profiles"] = {}
        n = len(data.get("profiles") or {})
        log.info("State loaded from %r (%d profile blob(s) on disk)", p, n)
        return data
    except Exception as e:
        log.error("State load failed (%s): %s", p, e)
        return {"init": False, "profiles": {}}


def _save(p: str, d: Dict):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        nprof = len(d.get("profiles") or {})
        log.debug("State saved path=%r profiles=%d", p, nprof)
    except Exception as e:
        log.error("State save failed: %s", e)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _audit_state_vs_config(state: Dict, profile_runtimes: List[ProfileRuntime]) -> None:
    active_keys = {pr.state_key for pr in profile_runtimes}
    blob = state.get("profiles") or {}
    stored_keys = set(blob.keys())
    orphans = stored_keys - active_keys
    if orphans:
        log.warning(
            "State has %d orphan profile key(s) not in this config (leftover from removed movies; harmless): %s",
            len(orphans),
            ", ".join(sorted(orphans)[:20]) + (" …" if len(orphans) > 20 else ""),
        )
    for pr in profile_runtimes:
        sub = blob.get(pr.state_key)
        if not sub:
            log.info("[%s] No prior state — fresh baseline for this profile_id", pr.profile_id)
            continue
        ho = sub.get("held_orders") or []
        st_map = sub.get("st") or {}
        log.info(
            "[%s] Restored state: held_orders=%d tracked_showtimes=%d first_scan_done(init)=%s",
            pr.profile_id,
            len(ho),
            len(st_map),
            sub.get("init", False),
        )
        if ho:
            for i, o in enumerate(ho[:5]):
                log.info(
                    "[%s]   hold[%d] seats=%s sid=%s movie=%s",
                    pr.profile_id,
                    i,
                    o.get("seats"),
                    o.get("sid"),
                    o.get("movie"),
                )
            if len(ho) > 5:
                log.info("[%s]   … +%d more held_orders", pr.profile_id, len(ho) - 5)


def _log_startup_runbook(cfg: Cfg, profile_runtimes: List[ProfileRuntime]) -> None:
    log.info(
        "Runbook: first releases — set --log-level DEBUG to log every showtime fetch failure + seat-layout miss. "
        "State path: %r (orphan keys are OK after removing movies).",
        cfg.state_path,
    )
    for pr in profile_runtimes:
        rd = _parse_release_date(pr.release_date)
        ps = _parse_release_date(pr.ticket_presale_start)
        phase = _profile_phase(pr, cfg.tz) if pr.movie_id else "waiting"
        log.info(
            "[%s] cfg snapshot movie=%r resolved_id=%s phase=%s release=%s presale=%s "
            "poll=%ds discovery=%ds workers=%d theatres=%d auto_reserve=%s max_orders=%d discount=%s mode=%s qty=%d",
            pr.profile_id,
            pr.movie,
            pr.movie_id or "—",
            phase,
            rd or "—",
            ps or "—",
            pr.poll,
            pr.discovery_interval,
            pr.workers,
            len(pr.theatres),
            pr.auto_reserve,
            pr.max_orders,
            pr.discount,
            pr.mode,
            pr.quantity,
        )


def status_dashboard(cfg: Cfg):
    client = AMCClient(timeout=cfg.timeout)
    resolve_config(cfg, client)
    found = discover_showtimes_legacy(cfg, client)
    disc_label = {"off": "OFF", "days": "Tue/Wed", "require": "Tue/Wed+eligible"}.get(cfg.discount, "OFF")
    print(f"\n{'═' * 80}")
    print(f"  {PRODUCT} — {cfg.movie_name or cfg.movie_slug} · {cfg.premium_label} @ {cfg.theatre_name}")
    print(f"  {len(found)} showtimes | {cfg.quantity}x | {cfg.tier} | discount={disc_label}")
    print(f"{'═' * 80}")

    pr = ProfileRuntime(
        movie_slug=cfg.movie_slug, quantity=cfg.quantity, tier=cfg.tier,
        min_score=cfg.min_score, target_seats=cfg.target_seats, workers=cfg.workers,
    )

    def fetch(st):
        good, groups = check_seats(pr, client, st)
        return st, len(good), groups

    with concurrent.futures.ThreadPoolExecutor(max_workers=pr.workers) as ex:
        for st, gc, gp in sorted(ex.map(fetch, found), key=lambda r: _dt(r[0], cfg.tz)):
            ic = "🟢" if gc > 10 else ("🟡" if gc > 0 else "🔴")
            best = f"{gp[0].names} ({gp[0].score}) {gp[0].tier}" if gp else ""
            disc = "💰" if st.discount_eligible and not st.discount_excluded else ("🚫" if st.discount_excluded else "  ")
            sold = "⚠️" if st.is_almost_sold_out else ("❌" if st.is_sold_out else "  ")
            price = st.price_label or ""
            print(f"  {ic} {st.date} {st.time:>5}{st.ampm:>3} {disc}{sold} {gc:>3}g {len(gp):>3}grp  {st.format_label:>12}  {price:>8}  {best}")
    print(f"\n{'═' * 80}")
    print(f"  💰=50% off  🚫=excluded from discount  ⚠️=almost sold out  ❌=sold out")
    print()


def startup_health_check(client: AMCClient) -> bool:
    log.info("Running startup health check...")
    h = client.health_check()
    if h.get("key_valid"):
        log.info("Health check passed: API reachable, key valid (%s)", h.get("sample_theatre", ""))
        return True
    log.warning("Health check issue: %s", h.get("error", "unknown"))
    if h.get("api_reachable"):
        log.warning("API is reachable but key may be invalid — continuing with GraphQL fallback")
        return True
    log.error("API unreachable — will rely entirely on GraphQL")
    return False


class ProfileRunner:
    def __init__(self, pr: ProfileRuntime, client: AMCClient, cfg: Cfg, global_state: Dict):
        self.pr = pr
        self.client = client
        self.cfg = cfg
        self.global_state = global_state
        self.state = global_state.setdefault("profiles", {}).setdefault(pr.state_key, {"init": False, "st": {}})
        if not isinstance(self.state.get("held_orders"), list):
            self.state["held_orders"] = []
        self.held_orders: List[Dict] = self.state["held_orders"]
        n_stale = _prune_stale_holds(self.held_orders, cfg.tz)
        if n_stale:
            log.info("[%s] Startup: pruned %d stale hold(s) from state", pr.profile_id, n_stale)
        self.engine: Optional[DiscoveryEngine] = None
        self.scan_count = 0
        self._stop = threading.Event()

    def run_once(self) -> int:
        if self.pr.mode == "release":
            return 0
        self.scan_count += 1
        t0 = time.monotonic()
        sts = self.engine.get_cached() if self.engine else discover_showtimes(self.pr, self.client, self.cfg)
        if not sts:
            log.debug(
                "[%s] Snipe scan #%d: 0 showtimes (discovery cache empty or no window)",
                self.pr.profile_id,
                self.scan_count,
            )
            return 0
        alerts = snipe_scan(self.pr, self.client, self.cfg, sts, self.state, self.held_orders)
        self.state["held_orders"] = self.held_orders
        if alerts:
            for a in alerts:
                r = a.get("reserved")
                log.info("[%s] %s → %s", self.pr.profile_id, a["st"].label,
                         f"RESERVED {r['seats']}" if r else f"{len(a.get('groups', []))} groups")
            threading.Thread(target=_notify_batch, args=(self.cfg, self.pr, alerts), daemon=True).start()
        log.info(
            "[%s] Scan #%d: %d showtime(s), %d alert(s), held_orders=%d, %.1fs",
            self.pr.profile_id,
            self.scan_count,
            len(sts),
            len(alerts),
            len(self.held_orders),
            time.monotonic() - t0,
        )
        return len(alerts)

    def start_discovery(self, initial: List[Showtime]):
        self.engine = DiscoveryEngine(self.pr, self.client, self.cfg, self.state, self.held_orders)
        self.engine.seed(initial)
        threading.Thread(target=self.engine.run, daemon=True, name=f"disc-{self.pr.profile_id}").start()

    def stop(self):
        self._stop.set()
        if self.engine:
            self.engine.stop()
        self.state["held_orders"] = self.held_orders


def _re_resolve_unresolved(unresolved: List[ProfileRuntime], client: AMCClient, cfg: Cfg) -> List[ProfileRuntime]:
    newly_resolved: List[ProfileRuntime] = []
    for pr in list(unresolved):
        try:
            m = client.resolve_movie(pr.movie)
            if m and m.get("id"):
                pr.movie_slug = m.get("slug", "")
                pr.movie_name = m.get("name", pr.movie)
                pr.movie_id = m.get("id", 0)
                pr.movie_image = m.get("poster_url", "")
                pr.release_date = m.get("release_date", "")
                pr.ticket_avail_date = m.get("ticket_avail_date", "")
                pr.run_time_minutes = int(m.get("run_time") or 0)
                phase = _profile_phase(pr, cfg.tz)
                rd = _parse_release_date(pr.release_date)
                log.info("[%s] Movie now available! %s → id=%s release=%s phase=%s", pr.profile_id, pr.movie, pr.movie_id, rd, phase)
                days_away = (rd - _now(cfg.tz).date()).days if rd else 0
                ps = _parse_release_date(pr.ticket_presale_start)

                desc = f"**{pr.movie_name}** is now listed on AMC.\n\n"
                if rd:
                    desc += f"📅 **Release:** `{_fmt_date(str(rd))}`"
                    if days_away > 0:
                        desc += f" — **{days_away} days away**"
                    desc += "\n"

                if phase == "dormant" and rd:
                    if ps:
                        desc += f"⏳ Presale monitoring starts **{_fmt_date(str(ps))}**.\n"
                    else:
                        desc += "💤 Entering **Dormant** — monitoring begins ~8 weeks before release.\n"
                    desc += "Metadata refreshes every 4 hours until then."
                elif phase == "watch":
                    desc += "👁️ Entering **Watch** phase — scanning for ticket on-sale drops."
                else:
                    desc += "🔥 Entering **Active** monitoring — full-speed scanning now."

                embed = _base_embed(f"🎬  Movie Found — {pr.movie_name}", 0x2ECC71, description=desc, url=_amc_movie_link(pr), pr=pr)
                embed["fields"] = _movie_info_fields(pr)
                _send_discord(cfg, {"embeds": [embed]})
                unresolved.remove(pr)
                newly_resolved.append(pr)
        except Exception as e:
            log.warning("[%s] Re-resolve attempt failed for %r: %s", pr.profile_id, pr.movie, e)
            log.debug("%s", traceback.format_exc())
    return newly_resolved


def run(cfg: Cfg, oneshot: bool = False) -> int:
    global _bot_start_time, _session_start_mono
    _reset_session_order_stats()
    _bot_start_time = _now(cfg.tz)
    _session_start_mono = time.monotonic()

    client = AMCClient(timeout=cfg.timeout)
    startup_health_check(client)

    profile_runtimes = cfg.build_profile_configs()
    if not profile_runtimes:
        log.error("No movie profiles configured. Set 'movie' or 'profiles' in config.")
        return 1

    for pr in profile_runtimes:
        try:
            resolve_profile(pr, client, cfg)
        except Exception as e:
            log.error("Profile %r resolution failed: %s\n%s", pr.movie, e, traceback.format_exc())

    resolved = [pr for pr in profile_runtimes if pr.movie_id]
    unresolved = [pr for pr in profile_runtimes if not pr.movie_id]

    _log_startup_runbook(cfg, profile_runtimes)

    if unresolved:
        raw_slugs = [pr.movie for pr in unresolved]
        log.warning("Movies not yet on AMC (will re-check periodically): %s", ", ".join(raw_slugs))
        desc = "These titles are **not listed on AMC yet**. The monitor retries in the background.\n\n"
        for pr in unresolved:
            readable = _slug_to_readable(pr.movie)
            desc += f"• **{readable}** `({pr.movie})`\n"
        re_check_mins = max(cfg.discovery_interval * 10, 300) // 60
        desc += f"\n🔄 **Background re-check:** every **{re_check_mins} minutes** (no extra Discord spam).\n"
        desc += "📣 **This webhook is sent once** when you start the monitor; you’ll get **Movie Found** when each title appears."
        embed = _base_embed("⏳  Waiting for Movies", 0xFFA500, description=desc)
        embed["fields"] = [
            {"name": "Pending", "value": f"`{len(raw_slugs)}` movie(s)", "inline": True},
            {"name": "Re-check Interval", "value": f"`{re_check_mins} min`", "inline": True},
            {"name": "Discord Frequency", "value": "`Once at startup`", "inline": True},
        ]
        _send_discord(cfg, {"embeds": [embed]})

    state = _load(cfg.state_path)
    _audit_state_vs_config(state, profile_runtimes)

    for pr in resolved:
        if pr.seating:
            try:
                test_pr = replace(
                    pr,
                    seating="",
                    scan_days=min(pr.scan_days, 7),
                    theatres=list(pr.theatres),
                    theatre_ids=dict(pr.theatre_ids),
                    theatre_names=dict(pr.theatre_names),
                )
                test_sts = discover_showtimes(
                    test_pr, client, cfg, seating_attr_probe=pr.seating
                )
                if not test_sts:
                    log.debug(
                        "[%s] Seating probe: 0 showtimes in current window — keeping seating=%r",
                        pr.profile_id,
                        pr.seating,
                    )
                    continue
                has_seating = any(
                    matches_seating(st.format_attrs, pr.seating, st.attribute_codes)
                    for st in test_sts
                )
                if not has_seating:
                    log.warning(
                        "[%s] No showtimes with seating '%s' among %d sampled — falling back to any",
                        pr.profile_id,
                        pr.seating,
                        len(test_sts),
                    )
                    fb_desc = f"No showtimes with **`{pr.seating}`** seating found for **{pr.movie_name or pr.movie}**.\n\n"
                    fb_desc += f"🪑 Falling back to **any seating type** to avoid missing showtimes.\n"
                    fb_desc += f"📍 Theatre: `{pr.theatre_name or pr.theatres[0] if pr.theatres else '—'}`"

                    embed = _base_embed(f"🪑  Seating Fallback — {pr.movie_name or pr.movie}", 0xFFA500, description=fb_desc, url=_amc_movie_link(pr), pr=pr)
                    embed["fields"] = _movie_info_fields(pr)
                    _send_discord(cfg, {"embeds": [embed]})
                    pr.seating = ""
            except Exception as e:
                log.warning("[%s] Seating check error: %s", pr.profile_id, e)

    runners: List[ProfileRunner] = []
    showtime_counts: Dict[str, int] = {}
    for pr in resolved:
        try:
            log.info("[%s] Initial discovery...", pr.profile_id)
            initial = discover_showtimes(pr, client, cfg)
            showtime_counts[pr.profile_id] = len(initial)
            runner = ProfileRunner(pr, client, cfg, state)
            if initial:
                runner.start_discovery(initial)
            elif not oneshot:
                runner.start_discovery([])
                log.warning("[%s] No showtimes found — discovery engine will keep searching", pr.profile_id)
            runners.append(runner)
        except Exception as e:
            log.error("[%s] Initial discovery failed: %s\n%s", pr.profile_id, e, traceback.format_exc())

    banner_lines = [f"{PRODUCT} v{VERSION}", f"Profiles: {len(resolved)} resolved, {len(unresolved)} waiting"]
    for pr in resolved:
        n = showtime_counts.get(pr.profile_id, 0)
        phase = _profile_phase(pr, cfg.tz)
        rd = _parse_release_date(pr.release_date)
        rd_s = str(rd) if rd else "?"
        banner_lines.append(
            f"  [{pr.profile_id}] {pr.movie_name or pr.movie} | {pr.format or 'All'} | "
            f"{pr.theatre_name} | {n} sts | {pr.mode} | phase={phase} | release={rd_s}"
        )
    for pr in unresolved:
        banner_lines.append(f"  [{pr.profile_id}] {pr.movie} | WAITING (not on AMC yet)")
    log.info("\n".join(banner_lines))

    if not oneshot:
        _discord_startup(cfg, resolved + unresolved, showtime_counts)

    release_runners = [r for r in runners if r.pr.mode == "release"]
    for runner in release_runners:
        initial = runner.engine.get_cached() if runner.engine else []
        if initial:
            log.info("[%s] Running release mode (startup)...", runner.pr.profile_id)
            t_r = time.monotonic()
            release_mode(runner.pr, client, cfg, initial, runner.held_orders, oneshot=oneshot)
            runner.state["held_orders"] = runner.held_orders
            if runner.engine:
                runner.engine._release_mode_fired = True
            if len(runner.held_orders) < runner.pr.max_orders:
                rel_elapsed = time.monotonic() - t_r
                log.info(
                    "RELEASE_FLOW [%s] RELEASE_TO_SNIPE transition (startup) orders=%d/%d elapsed=%s",
                    runner.pr.profile_id,
                    len(runner.held_orders),
                    runner.pr.max_orders,
                    _format_duration(rel_elapsed),
                )
                _discord_mode_release_to_snipe(
                    cfg,
                    runner.pr,
                    orders_held=len(runner.held_orders),
                    release_elapsed_sec=rel_elapsed,
                    source="startup_initial_showtimes",
                    showtimes_in_release=len(initial),
                )
                runner.pr.mode = "snipe"
        else:
            log.info(
                "[%s] Release mode deferred — no showtimes at startup; discovery will batch-fire on first drop",
                runner.pr.profile_id,
            )

    snipe_runners = list(runners)
    if not snipe_runners and not runners and not unresolved:
        log.warning("No profiles to run.")
        return 0

    shutting_down = threading.Event()
    total_scans = 0
    last_re_resolve = time.monotonic()
    last_auto_save = time.monotonic()
    AUTO_SAVE_INTERVAL = 300
    re_resolve_interval = max(cfg.discovery_interval * 10, 300)

    def on_sig(sig, frame):
        if not shutting_down.is_set():
            log.info("Shutdown signal received — stopping runners, saving state, exiting")
            shutting_down.set()
            for r in runners:
                r.stop()
            _save(cfg.state_path, state)
            all_orders = []
            for r in runners:
                all_orders.extend(r.held_orders)
            _discord_shutdown(cfg, total_scans, all_orders, runners=runners)
            sys.exit(0)

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    errs = 0
    while not shutting_down.is_set():
        total_scans += 1
        log.info("━━━ Scan #%d ━━━", total_scans)
        t0 = time.monotonic()

        if unresolved and (t0 - last_re_resolve) >= re_resolve_interval:
            last_re_resolve = t0
            log.info("Re-checking %d unresolved movie(s)...", len(unresolved))
            newly = _re_resolve_unresolved(unresolved, client, cfg)
            for pr in newly:
                try:
                    resolve_profile(pr, client, cfg)
                    initial = discover_showtimes(pr, client, cfg)
                    showtime_counts[pr.profile_id] = len(initial)
                    runner = ProfileRunner(pr, client, cfg, state)
                    runner.start_discovery(initial if initial else [])
                    runners.append(runner)
                    if pr.mode == "release" and initial:
                        t_r = time.monotonic()
                        release_mode(pr, client, cfg, initial, runner.held_orders, oneshot=oneshot)
                        runner.state["held_orders"] = runner.held_orders
                        if runner.engine:
                            runner.engine._release_mode_fired = True
                        if len(runner.held_orders) < pr.max_orders:
                            _discord_mode_release_to_snipe(
                                cfg,
                                pr,
                                orders_held=len(runner.held_orders),
                                release_elapsed_sec=time.monotonic() - t_r,
                                source="movie_newly_listed",
                                showtimes_in_release=len(initial),
                            )
                            pr.mode = "snipe"
                    snipe_runners.append(runner)
                except Exception as e:
                    log.error("[%s] Newly resolved setup failed: %s", pr.profile_id, e)

        try:
            for runner in snipe_runners:
                try:
                    runner.run_once()
                except Exception as e:
                    log.error("[%s] Scan error: %s\n%s", runner.pr.profile_id, e, traceback.format_exc())
            now_mono = time.monotonic()
            if now_mono - last_auto_save >= AUTO_SAVE_INTERVAL:
                _save(cfg.state_path, state)
                last_auto_save = now_mono
            errs = 0
            log.info("Scan #%d complete: %.1fs", total_scans, time.monotonic() - t0)
        except Exception as e:
            errs += 1
            log.error("Scan error (%d): %s\n%s", errs, e, traceback.format_exc())
            if errs >= 10:
                log.error("Too many consecutive errors — cooling down 30s")
                time.sleep(30)
                errs = 0

        if oneshot:
            break
        min_poll = min(r.pr.poll for r in runners) if runners else cfg.poll
        time.sleep(max(min_poll + random.uniform(-0.5, 0.5), 1))

    for r in runners:
        r.stop()
    _save(cfg.state_path, state)
    if not oneshot:
        all_orders = []
        for r in runners:
            all_orders.extend(r.held_orders)
        _discord_shutdown(cfg, total_scans, all_orders, runners=runners)
    return 0


def _banner_rgb(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"


def _banner_hx(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_BANNER_GRAD = ["#A8281F", "#C9302C", "#D94843", "#EC6A5F"]
_BANNER_ACCENT = "#C9302C"
_BANNER_VERSION = "v5.23.25"
_BANNER_AUTHOR = "habib"

_BANNER_AMC = r"""
 █████╗ ███╗   ███╗ ██████╗     ██╗
██╔══██╗████╗ ████║██╔════╝     ██║
███████║██╔████╔██║██║       ████████╗
██╔══██║██║╚██╔╝██║██║       ╚══██╔══╝
██║  ██║██║ ╚═╝ ██║╚██████╗      ██║
╚═╝  ╚═╝╚═╝     ╚═╝ ╚═════╝      ╚═╝
""".strip("\n").splitlines()


def _banner_grad_at(t, stops=_BANNER_GRAD):
    if t <= 0:
        return _banner_hx(stops[0])
    if t >= 1:
        return _banner_hx(stops[-1])
    seg = t * (len(stops) - 1)
    i = int(seg)
    f = seg - i
    a = _banner_hx(stops[i])
    b = _banner_hx(stops[i + 1])
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def _banner_paint(line, t0, t1):
    width = max(1, len(line) - 1)
    out = []
    for i, ch in enumerate(line):
        if ch == " ":
            out.append(" ")
            continue
        t = t0 + (i / width) * (t1 - t0)
        r, g, b = _banner_grad_at(t)
        out.append(f"{_banner_rgb(r, g, b)}{ch}")
    return "".join(out) + "\033[0m"


def print_banner() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print()
    rows = len(_BANNER_AMC)
    for i, ln in enumerate(_BANNER_AMC):
        t0 = 0.00 + (i / max(1, rows - 1)) * 0.15
        t1 = 0.85 + (i / max(1, rows - 1)) * 0.15
        print("  " + _banner_paint(ln, t0, t1))
    r, g, b = _banner_hx(_BANNER_ACCENT)
    reset = "\033[0m"
    dim = "\033[2m"
    bold = "\033[1m"
    rule = "\u2500" * 60
    print()
    print(f"  {dim}{rule}{reset}  {_banner_rgb(r, g, b)}\u25C6{reset}  {dim}{rule}{reset}")
    print()
    dot = f"{dim}\u00B7{reset}"
    brand = f"{_banner_rgb(r, g, b)}{bold}AMC+{reset}"
    print(f"   {brand} {dot} Stream Monitor")
    meta = f"{dim}VERSION{reset} {_BANNER_VERSION}  {dot}  {dim}AUTHOR{reset} {_BANNER_AUTHOR}"
    print(f"   {meta}")
    print()


def main(argv=None):
    p = argparse.ArgumentParser(description=f"{PRODUCT} v{VERSION}")
    p.add_argument("--config", "-c", default="./config.jsonc")
    p.add_argument("--oneshot", "-1", action="store_true")
    p.add_argument("--status", "-s", action="store_true")
    p.add_argument("--reset-state", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    print_banner()
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, args.log_level.upper(), logging.INFO),
    )
    if not os.path.exists(args.config):
        log.error("Config not found: %s", args.config)
        return 1
    cfg = Cfg.from_json(args.config)
    if args.reset_state and os.path.exists(cfg.state_path):
        os.remove(cfg.state_path)
        log.info("State reset.")
    if args.status:
        status_dashboard(cfg)
        return 0
    return run(cfg, oneshot=args.oneshot)


if __name__ == "__main__":
    sys.exit(main())