from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("amc.api")


BASE = "https://www.amctheatres.com"
API_BASE = "https://dontwannatellyou.com"
GRAPHQL = "https://canttellyou.com/"
_DEFAULT_VENDOR_KEY = "thisisasecret"

_DEFAULT_GQL_BEARER = "thisisanothersecret"


def _graphql_device_id() -> str:
    env = (os.environ.get("AMC_DEVICE_ID") or "").strip()
    if env:
        return env
    state_dir = os.environ.get("AMC_STATE_DIR", "./state")
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, ".amc_gql_device_id")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
        did = uuid.uuid4().hex[:16]
        with open(path, "w", encoding="utf-8") as f:
            f.write(did)
        return did
    except OSError:
        return uuid.uuid4().hex[:16]


def build_gql_headers() -> Dict[str, str]:
    app_ver = (os.environ.get("AMC_APP_VERSION") or "there is no app version trust bro").strip()
    bearer = (
        os.environ.get("AMC_GQL_BEARER")
        or os.environ.get("AMC_BEARER")
        or _DEFAULT_GQL_BEARER
    ).strip()
    device_id = _graphql_device_id()
    ua = (
        os.environ.get("AMC_GQL_USER_AGENT")
        or f"AMCTheatres/{app_ver} (i wonder what i put here)"
    )
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {bearer}",
        "User-Agent": ua,
        "Origin": "https://www.amctheatres.com",
        "Referer": "https://www.amctheatres.com/",
        "x-amc-device-os-type": "android",
        "x-amc-device-app-version": app_ver,
        "x-amc-device-id": device_id,
        "x-amc-device-os-version": "15",
        "Connection": "keep-alive",
        "apollographql-client-name": "com.amc",
        "apollographql-client-version": app_ver,
    }


GQL_HEADERS = build_gql_headers()
HEADERS = GQL_HEADERS


def _gql_transport_backend():
    impersonate = (os.environ.get("AMC_GQL_IMPERSONATE") or "chrome_android").strip()
    try:
        import curl_cffi.requests as cf_requests

        return "curl_cffi", cf_requests, impersonate
    except ImportError:
        return "requests", requests, ""

DISCOUNT_DAYS = {1, 2}
_RE_SNAME = re.compile(r"([A-Za-z]+)(\d+)")
_RE_ENGLISH_SUB = re.compile(r"WITH\s+ENGLISH\s+SUBTITLES", re.IGNORECASE)
_RE_SLUG_ID = re.compile(r"^.+-(\d{3,})$")


class AMCAPIError(Exception):

    def __init__(self, status: int, url: str, detail: str = ""):
        self.status = status
        self.url = url
        self.detail = detail[:500]
        super().__init__(f"HTTP {status} {url}: {self.detail}")


class AMCNotFoundError(AMCAPIError):
    pass


def flatten_theatre_record(t: Optional[Dict]) -> Optional[Dict]:
    if not t:
        return None
    loc = t.get("location") or {}
    line1 = (t.get("addressLine1") or loc.get("addressLine1") or "").strip()
    city = (t.get("city") or loc.get("city") or "").strip()
    st = (
        t.get("stateCode")
        or t.get("state")
        or loc.get("state")
        or loc.get("stateCode")
        or ""
    )
    st = str(st).strip()
    z = (t.get("postalCode") or loc.get("postalCode") or "").strip()
    tid = t.get("id")
    if tid is None:
        tid = t.get("theatreId")
    try:
        tid_int = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        tid_int = 0
    return {
        "id": tid_int,
        "slug": (t.get("slug") or "").strip(),
        "name": (t.get("name") or "").strip(),
        "addressLine1": line1,
        "city": city,
        "stateCode": st,
        "postalCode": z,
    }


def merge_theatre_records(a: Optional[Dict], b: Optional[Dict]) -> Optional[Dict]:
    fa = flatten_theatre_record(a) if a else None
    fb = flatten_theatre_record(b) if b else None
    if not fa:
        return fb
    if not fb:
        return fa
    out = dict(fa)
    for k in ("id", "slug", "name", "addressLine1", "city", "stateCode", "postalCode"):
        if not out.get(k) and fb.get(k):
            out[k] = fb[k]
    return out


class AMCRateLimitError(AMCAPIError):
    pass


class AMCAuthError(AMCAPIError):
    pass


FORMAT_CODES: Dict[str, Set[str]] = {
    "IMAX 70MM": {"IMAX70MM"},
    "IMAX": {"IMAX"},
    "DOLBY": {"DOLBYCINEMAATAMCPRIME"},
    "LASER": {"LASERATAMC"},
    "REALD 3D": {"REALD3D"},
    "PRIME 3D": {"PRIME3D"},
}

SEATING_CODES: Dict[str, Set[str]] = {
    "recliners": {"RECLINERSEATING"},
    "rockers": {"AMCCLUBROCKERS"},
}

PREMIUM_CODE_SET = frozenset(
    {"IMAX70MM", "IMAX", "70MM", "DOLBYCINEMAATAMCPRIME", "LASERATAMC", "REALD3D", "PRIME3D"}
)


_TARGET_ROW_FRAC = 0.62
_ROW_W = 0.58
_CENTER_W = 0.42
_FRONT_PEN_ROWS = 2
_FRONT_PEN = 18.0
_BACK_PEN_ROWS = 1
_BACK_PEN = 6.0
_EDGE_PEN_COLS = 4
_EDGE_PEN = 7.0
_ACCESSIBLE_PEN = 12.0
_MIN_GOOD = 55.0


def score_seat(row: int, col: int, rows: int, cols: int) -> float:
    ideal_row = rows * _TARGET_ROW_FRAC + 1
    s = 100.0 * (
        1.0
        - (
        _ROW_W * abs(row - ideal_row) / max(rows, 1)
        + _CENTER_W * abs(col - (cols + 1) / 2) / max(cols / 2, 1)
        )
    )
    if row <= _FRONT_PEN_ROWS:
        s -= _FRONT_PEN
    if row >= rows - _BACK_PEN_ROWS + 1:
        s -= _BACK_PEN
    if col <= _EDGE_PEN_COLS or col >= cols - _EDGE_PEN_COLS + 1:
        s -= _EDGE_PEN
    return round(s, 2)


def tier_label(s: float) -> str:
    if s >= 80:
        return "🏆 ELITE"
    if s >= 70:
        return "⭐ GREAT"
    if s >= 60:
        return "✅ GOOD"
    if s >= 55:
        return "🟡 OK"
    return ""


def seat_sort_key(n: str) -> Tuple[str, int]:
    m = _RE_SNAME.match(n or "")
    return (m.group(1), int(m.group(2))) if m else (n or "", 0)


@dataclass
class Showtime:
    sid: int
    date: str
    time: str
    ampm: str
    status: str
    movie_slug: str = ""
    movie_name: str = ""
    movie_id: int = 0
    auditorium: int = 0
    format_attrs: List[str] = field(default_factory=list)
    attribute_codes: List[str] = field(default_factory=list)
    discount_eligible: Optional[bool] = None
    ticket_prices: List[Dict] = field(default_factory=list)
    is_sold_out: bool = False
    is_almost_sold_out: bool = False
    performance_number: int = 0
    theatre_id: int = 0
    theatre_name: str = ""
    theatre_slug: str = ""
    premium_format: str = ""
    show_datetime_utc: str = ""
    show_datetime_local: str = ""

    @property
    def url(self):
        return f"{BASE}/showtimes/{self.sid}/seats"

    @property
    def ticket_url(self):
        return f"{BASE}/showtimes/{self.sid}/seats?seats="

    @property
    def label(self):
        return f"{self.date} {self.time} {self.ampm}"

    @property
    def format_label(self) -> str:
        codes = set(self.attribute_codes)
        if "IMAX70MM" in codes:
            return "IMAX 70MM"
        if "DOLBYCINEMAATAMCPRIME" in codes:
            return "Dolby Cinema"
        if "LASERATAMC" in codes:
            return "Laser"
        if "PRIME3D" in codes:
            return "PRIME 3D"
        if "REALD3D" in codes:
            return "RealD 3D"
        if "IMAX" in codes:
            return "IMAX"
        for a in self.format_attrs:
            au = a.upper()
            if "IMAX" in au and "70" in au:
                return "IMAX 70MM"
            if "DOLBY" in au:
                return "Dolby Cinema"
            if "PRIME" in au and "3D" in au:
                return "PRIME 3D"
            if "REALD" in au:
                return "RealD 3D"
            if "IMAX" in au:
                return "IMAX"
            if "LASER" in au:
                return "Laser"
        if self.premium_format:
            return self.premium_format
        return "Standard"

    @property
    def is_open_caption(self) -> bool:
        if "OPENCAPTION" in self.attribute_codes:
            return True
        return any(
            "open caption" in a.lower() or "on-screen subtitle" in a.lower()
            for a in self.format_attrs
        )

    @property
    def has_english_subtitles(self) -> bool:
        for a in self.format_attrs:
            if _RE_ENGLISH_SUB.search(a):
                return True
        if self.movie_name and _RE_ENGLISH_SUB.search(self.movie_name):
            return True
        return False

    @property
    def seating_type(self) -> str:
        codes = set(self.attribute_codes)
        if "RECLINERSEATING" in codes:
            return "recliners"
        if any(c and "RECLINER" in c.upper() for c in self.attribute_codes):
            return "recliners"
        if "AMCCLUBROCKERS" in codes:
            return "rockers"
        if any(c and "ROCKER" in c.upper() for c in self.attribute_codes):
            return "rockers"
        for a in self.format_attrs:
            al = a.lower()
            if "signature recliner" in al or "recliner seating" in al:
                return "recliners"
            if "recliner" in al and "non-reclin" not in al:
                return "recliners"
            if "club rocker" in al:
                return "rockers"
            if "rocker" in al and "non-" not in al:
                return "rockers"
        return "standard"

    @property
    def discount_excluded(self) -> bool:
        if "EXCLDISDAY" in self.attribute_codes:
            return True
        return any("excluded from 50%" in a.lower() for a in self.format_attrs)

    @property
    def price_label(self) -> str:
        if not self.ticket_prices:
            return ""
        adult = next((p for p in self.ticket_prices if p.get("type") == "ADULT"), None)
        if adult:
            return f"${adult['price']:.2f}"
        if self.ticket_prices:
            return f"${self.ticket_prices[0].get('price', 0):.2f}"
        return ""

    @classmethod
    def from_rest(
        cls,
        data: Dict,
        theatre_display_name: str = "",
        *,
        theatre_slug: str = "",
    ) -> "Showtime":
        dt_local = data.get("showDateTimeLocal", "")
        date_str, time_str, ampm = "", "", ""
        if dt_local:
            try:
                dt = datetime.fromisoformat(dt_local)
                date_str = dt.strftime("%Y-%m-%d")
                time_str = dt.strftime("%I:%M").lstrip("0")
                ampm = dt.strftime("%p")
            except ValueError:
                pass
        attrs = data.get("attributes") or []
        return cls(
            sid=data.get("id", 0),
            date=date_str,
            time=time_str,
            ampm=ampm,
            status="CANCELED" if data.get("isCanceled") else "ACTIVE",
            movie_slug=str(data.get("movieId", "")),
            movie_name=data.get("movieName", ""),
            movie_id=data.get("movieId", 0),
            auditorium=data.get("auditorium", 0),
            format_attrs=[a.get("name", "") for a in attrs if a.get("name")],
            attribute_codes=[a.get("code", "") for a in attrs if a.get("code")],
            discount_eligible=data.get("isDiscountDaysEligible"),
            ticket_prices=data.get("ticketPrices") or [],
            is_sold_out=data.get("isSoldOut", False),
            is_almost_sold_out=data.get("isAlmostSoldOut", False),
            performance_number=data.get("performanceNumber", 0),
            theatre_id=data.get("theatreId", 0),
            theatre_name=theatre_display_name or theatre_slug,
            theatre_slug=theatre_slug,
            premium_format=data.get("premiumFormat", ""),
            show_datetime_utc=data.get("showDateTimeUtc", ""),
            show_datetime_local=dt_local,
        )


RESERVABLE_TYPES = frozenset({"CanReserve", "LoveSeatLeft", "LoveSeatRight"})
LOVESEAT_TYPES = frozenset({"LoveSeatLeft", "LoveSeatRight"})
_LOVESEAT_BONUS = 3.0


@dataclass
class Seat:
    name: str
    row: int
    col: int
    stype: str
    score: float = 0.0
    tier: str = ""

    @property
    def is_loveseat(self) -> bool:
        return self.stype in LOVESEAT_TYPES

    @property
    def is_loveseat_left(self) -> bool:
        return self.stype == "LoveSeatLeft"

    @property
    def is_loveseat_right(self) -> bool:
        return self.stype == "LoveSeatRight"


def _is_loveseat_pair(a: Seat, b: Seat) -> bool:
    return (
        a.is_loveseat_left
        and b.is_loveseat_right
        and a.row == b.row
        and b.col == a.col + 1
    )


def _is_all_loveseat_pairs(seats: List["Seat"]) -> bool:
    if len(seats) % 2 != 0:
        return False
    for i in range(0, len(seats), 2):
        if not _is_loveseat_pair(seats[i], seats[i + 1]):
            return False
    return True


@dataclass
class SeatGroup:
    sid: int
    seats: List[Seat]
    score: float
    block_size: int
    is_loveseat_pair: bool = False

    @property
    def id(self):
        return "|".join(sorted([s.name for s in self.seats], key=seat_sort_key))

    @property
    def names(self):
        return " + ".join(sorted([s.name for s in self.seats], key=seat_sort_key))

    @property
    def query(self):
        return ",".join(sorted([s.name for s in self.seats], key=seat_sort_key))

    @property
    def tier(self):
        return tier_label(self.score)

    @property
    def coords(self):
        return [(s.row, s.col) for s in self.seats]


def parse_seats(
    layout: Dict, include_accessible: bool = False
) -> Tuple[List[Seat], int, int]:
    rows = max(int(layout.get("rows") or 1), 1)
    cols = max(int(layout.get("columns") or 1), 1)
    out: List[Seat] = []
    for s in layout.get("seats") or []:
        if not s.get("available") or not s.get("shouldDisplay"):
            continue
        st = s.get("type", "")
        if st in RESERVABLE_TYPES:
            pass
        elif include_accessible and st in ("Wheelchair", "Companion"):
            pass
        else:
            continue
        r, c = int(s["row"]), int(s["column"])
        v = score_seat(r, c, rows, cols)
        if st not in RESERVABLE_TYPES:
            v -= _ACCESSIBLE_PEN
        v = round(v, 2)
        out.append(Seat(s.get("name", ""), r, c, st, v, tier_label(v)))
    return out, rows, cols


def find_groups(
    sid: int, seats: List[Seat], rows: int, cols: int, min_size: int = 2,
    require_loveseat_pairs: bool = False,
) -> List[SeatGroup]:
    by_row: Dict[int, List[Seat]] = defaultdict(list)
    for s in seats:
        by_row[s.row].append(s)
    groups: List[SeatGroup] = []
    for row_seats in by_row.values():
        row_seats.sort(key=lambda s: s.col)
        blocks: List[List[Seat]] = [[]]
        for seat in row_seats:
            if blocks[-1] and seat.col != blocks[-1][-1].col + 1:
                blocks.append([])
            blocks[-1].append(seat)
        for blk in blocks:
            if len(blk) < min_size:
                continue
            for i in range(len(blk) - min_size + 1):
                w = blk[i : i + min_size]
                is_all_lp = _is_all_loveseat_pairs(w) if len(w) % 2 == 0 else False
                if require_loveseat_pairs and len(w) % 2 == 0 and not is_all_lp:
                    continue
                avg_col = sum(s.col for s in w) / len(w)
                ps = score_seat(w[0].row, avg_col, rows, cols)
                if any(s.stype not in RESERVABLE_TYPES for s in w):
                    ps -= _ACCESSIBLE_PEN
                if is_all_lp:
                    ps += _LOVESEAT_BONUS
                groups.append(SeatGroup(sid, w, round(ps, 2), len(blk), is_loveseat_pair=is_all_lp))
    groups.sort(key=lambda g: (g.is_loveseat_pair, g.score), reverse=True)
    return groups


def non_overlapping(groups: List[SeatGroup], limit: int = 5) -> List[SeatGroup]:
    used: set = set()
    out: List[SeatGroup] = []
    for g in groups:
        names = frozenset(s.name for s in g.seats)
        if not names & used:
            out.append(g)
            used |= names
            if len(out) >= limit:
                break
    return out


def matches_format(
    attrs: List[str], target: str, codes: Optional[List[str]] = None
) -> bool:
    if not target or target.upper() in ("ALL", "ANY", ""):
        return True
    tf = target.upper().strip()

    if codes:
        code_set = set(codes)
        if tf == "IMAX 70MM":
            if "IMAX70MM" in code_set:
                return True
            if "IMAX" in code_set and "70MM" in code_set:
                return True
            return False
        if tf == "IMAX":
            return "IMAX" in code_set and "DOLBYCINEMAATAMCPRIME" not in code_set and "IMAX70MM" not in code_set
        if tf in FORMAT_CODES and FORMAT_CODES[tf] & code_set:
            return True
        if tf in ("STANDARD", "DIGITAL"):
            return not bool(code_set & PREMIUM_CODE_SET)

    for a in attrs:
        au = a.upper()
        if tf == "IMAX 70MM" and "IMAX" in au and "70" in au:
            return True
        if tf == "IMAX" and "IMAX" in au and "DOLBY" not in au:
            return True
        if tf == "DOLBY" and "DOLBY" in au:
            return True
        if tf == "LASER" and "LASER" in au and "IMAX" not in au:
            return True
        if tf == "PRIME 3D" and "PRIME" in au and "3D" in au:
            return True
        if tf == "REALD 3D" and "REALD" in au:
            return True
        if tf == au:
            return True
    if tf in ("STANDARD", "DIGITAL"):
        return not any(
            kw in a.upper()
            for a in attrs
            for kw in ("IMAX", "DOLBY", "LASER", "PRIME", "REALD")
        )
    return False


def matches_seating(
    attrs: List[str], target: str, codes: Optional[List[str]] = None
) -> bool:
    if not target or target.lower() in ("any", ""):
        return True
    tl = target.lower()

    if codes and tl in SEATING_CODES:
        if SEATING_CODES[tl] & set(codes):
            return True
    if codes and tl == "recliners":
        if any(c and "RECLINER" in c.upper() for c in codes):
            return True
    if codes and tl == "rockers":
        if any(c and "ROCKER" in c.upper() for c in codes):
            return True

    for a in attrs:
        al = a.lower()
        if tl == "recliners":
            if "signature recliner" in al or "recliner seating" in al:
                return True
            if "recliner" in al and "non-reclin" not in al:
                return True
        if tl == "rockers":
            if "club rocker" in al:
                return True
            if "rocker" in al and "non-" not in al:
                return True
    return False


def _has_any_subtitles(attrs: List[str], codes: Optional[List[str]] = None, movie_name: str = "") -> bool:
    if codes and "OPENCAPTION" in codes:
        return True
    for a in attrs:
        al = a.lower()
        if "open caption" in al or "on-screen subtitle" in al:
            return True
        if _RE_ENGLISH_SUB.search(a):
            return True
    if movie_name and _RE_ENGLISH_SUB.search(movie_name):
        return True
    return False


def matches_subtitles(
    attrs: List[str],
    target: str,
    codes: Optional[List[str]] = None,
    movie_name: str = "",
) -> bool:
    if not target:
        return not _has_any_subtitles(attrs, codes, movie_name)

    tl = target.lower().strip()

    if tl in ("any", "all"):
        return True

    if tl == "open_caption":
        if codes and "OPENCAPTION" in codes:
            return True
        return any(
            "open caption" in a.lower() or "on-screen subtitle" in a.lower()
            for a in attrs
        )

    if tl == "english_subtitles":
        for a in attrs:
            if _RE_ENGLISH_SUB.search(a):
                return True
        if movie_name and _RE_ENGLISH_SUB.search(movie_name):
            return True
        return False

    return True


class _TTLCache:

    def __init__(self, ttl: float = 300.0, max_size: int = 2048):
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
            if entry:
                del self._data[key]
        return None

    def put(self, key: str, value: Any):
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            if len(self._data) > self._max_size:
                self._evict()

    def _evict(self):
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._data.items() if now - ts >= self._ttl]
        for k in expired:
            del self._data[k]
        if len(self._data) > self._max_size:
            by_age = sorted(self._data.items(), key=lambda kv: kv[1][0])
            for k, _ in by_age[: len(self._data) - self._max_size]:
                del self._data[k]

    def clear(self):
        with self._lock:
            self._data.clear()

    def __len__(self):
        with self._lock:
            now = time.monotonic()
            return sum(1 for _, (ts, _) in self._data.items() if now - ts < self._ttl)


class OfficialAMCClient:

    def __init__(
        self,
        vendor_key: str = "",
        timeout: int = 12,
        pool: int = 36,
        max_retries: int = 3,
        cache_ttl: float = 300.0,
    ):
        self._vendor_key = (
            vendor_key or os.environ.get("AMC_VENDOR_KEY", _DEFAULT_VENDOR_KEY)
        )
        self._timeout = timeout if timeout and timeout > 0 else 12
        self._max_retries = max_retries
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=pool, pool_maxsize=pool, max_retries=0
        )
        self._session.mount("https://", adapter)
        self._session.headers.update(
            {
                "X-AMC-Vendor-Key": self._vendor_key,
                "Accept": "application/json",
                "User-Agent": "AMCPlus/5.0",
            }
        )
        self._movie_cache = _TTLCache(cache_ttl)
        self._theatre_cache = _TTLCache(cache_ttl)
        self._failure_counts: Dict[str, int] = defaultdict(int)
        self._failure_lock = threading.Lock()


    def _request(
        self, method: str, path: str, params: Optional[Dict] = None
    ) -> Optional[Dict]:
        url = f"{API_BASE}/{path.lstrip('/')}"
        cid = uuid.uuid4().hex[:8]
        last_exc: Optional[Exception] = None

        with self._failure_lock:
            endpoint_key = path.split("?")[0]
            if self._failure_counts.get(endpoint_key, 0) >= 20:
                log.warning(
                    "[%s] Circuit open for %s — skipping", cid, endpoint_key
                )
                self._failure_counts[endpoint_key] = max(
                    0, self._failure_counts[endpoint_key] - 1
                )
                return None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = min(2**attempt, 8) + random.uniform(0, 0.5)
                log.info(
                    "[%s] Retry %d/%d for %s after %.1fs",
                    cid, attempt, self._max_retries, path, delay,
                )
                time.sleep(delay)

            t0 = time.monotonic()
            try:
                r = self._session.request(
                    method, url, params=params, timeout=self._timeout
                )
                elapsed = time.monotonic() - t0
                log.debug(
                    "[%s] %s %s → %d (%.2fs)", cid, method, path, r.status_code, elapsed
                )

                if r.status_code == 200:
                    with self._failure_lock:
                        self._failure_counts[endpoint_key] = 0
                    return r.json()

                if r.status_code == 404:
                    return None

                if r.status_code == 429:
                    retry_after = float(r.headers.get("Retry-After", 5))
                    log.warning(
                        "[%s] Rate limited on %s — backing off %.1fs",
                        cid, path, retry_after,
                    )
                    time.sleep(retry_after)
                    last_exc = AMCRateLimitError(429, url, "rate limited")
                    continue

                if r.status_code in (401, 403):
                    log.error(
                        "[%s] Auth rejected %d on %s", cid, r.status_code, path
                    )
                    raise AMCAuthError(r.status_code, url, r.text[:200])

                if r.status_code >= 500:
                    log.warning(
                        "[%s] Server error %d on %s (attempt %d)",
                        cid, r.status_code, path, attempt + 1,
                    )
                    last_exc = AMCAPIError(r.status_code, url, r.text[:200])
                    continue

                raise AMCAPIError(r.status_code, url, r.text[:200])

            except (requests.ConnectionError, requests.Timeout) as e:
                elapsed = time.monotonic() - t0
                log.warning(
                    "[%s] %s on %s (%.2fs, attempt %d)",
                    cid, type(e).__name__, path, elapsed, attempt + 1,
                )
                last_exc = e

        with self._failure_lock:
            self._failure_counts[endpoint_key] = (
                self._failure_counts.get(endpoint_key, 0) + 1
            )

        raise AMCAPIError(
            0, url, f"All {self._max_retries + 1} attempts failed"
        ) from last_exc


    def search_movies(self, query: str, page_size: int = 20) -> List[Dict]:
        data = self._request(
            "GET", "/v2/movies", {"name": query, "page-size": page_size}
        )
        if not data:
            return []
        return (data.get("_embedded") or {}).get("movies") or []

    def get_movie_by_id(self, movie_id: int) -> Optional[Dict]:
        return self._request("GET", f"/v2/movies/{movie_id}")

    def resolve_movie(self, query: str) -> Optional[Dict]:
        cached = self._movie_cache.get(query)
        if cached is not None:
            return cached

        if query.strip().isdigit():
            m = self.get_movie_by_id(int(query.strip()))
            if m:
                info = self._movie_info(m)
                self._movie_cache.put(query, info)
                return info

        slug_m = _RE_SLUG_ID.match(query)
        if slug_m:
            m = self.get_movie_by_id(int(slug_m.group(1)))
            if m:
                info = self._movie_info(m)
                self._movie_cache.put(query, info)
                return info

        clean = query.replace("-", " ").replace("_", " ").strip()
        results = self.search_movies(clean)
        if results:
            best = self._pick_best_match(clean, results)
            if best and self._fuzzy(clean, best.get("name", "")) >= 40:
                info = self._movie_info(best)
                self._movie_cache.put(query, info)
                return info

        if clean != query:
            results = self.search_movies(query)
            if results:
                best = self._pick_best_match(query, results)
                if best:
                    info = self._movie_info(best)
                    self._movie_cache.put(query, info)
                    return info

        found = self._search_subphrases(query, clean)
        if found:
            self._movie_cache.put(query, found)
            return found

        log.warning("Could not resolve movie: %r", query)
        return None

    def _search_subphrases(self, original: str, clean: str) -> Optional[Dict]:
        words = clean.split()
        if len(words) <= 2:
            return None

        seen_queries: Set[str] = {clean}
        candidates: List[Dict] = []

        for window in range(len(words) - 1, 1, -1):
            for start in reversed(range(len(words) - window + 1)):
                sub = " ".join(words[start : start + window])
                if sub in seen_queries:
                    continue
                seen_queries.add(sub)
                results = self.search_movies(sub)
                if not results:
                    continue
                for r in results:
                    score = self._fuzzy(clean, r.get("name", ""))
                    if score >= 60:
                        candidates.append(r)
                if candidates:
                    best = self._pick_best_match(clean, candidates)
                    if best:
                        log.info(
                            "Sub-phrase search %r found %r (id=%s) for original query %r",
                            sub, best.get("name"), best.get("id"), original,
                        )
                        return self._movie_info(best)

        return None

    @staticmethod
    def _movie_info(m: Dict) -> Dict:
        poster = ""
        media = m.get("media") or {}
        if media.get("posterDynamic"):
            poster = media["posterDynamic"]
        elif (m.get("preferredPoster") or {}).get("url"):
            poster = m["preferredPoster"]["url"]
        return {
            "id": m.get("id", 0),
            "name": m.get("name", ""),
            "slug": m.get("slug", ""),
            "poster_url": poster,
            "release_date": m.get("releaseDateUtc", ""),
            "ticket_avail_date": m.get("onlineTicketAvailabilityDateUtc", ""),
            "status": m.get("status", ""),
            "mpaa_rating": m.get("mpaaRating", ""),
            "genre": m.get("genre", ""),
            "run_time": m.get("runTime", 0),
            "has_scheduled_showtimes": m.get("hasScheduledShowtimes", False),
        }

    @staticmethod
    def _fuzzy(query: str, candidate: str) -> int:
        q = query.lower().strip()
        c = candidate.lower().strip()
        if q == c:
            return 100
        qn = re.sub(r"[^a-z0-9]", "", q)
        cn = re.sub(r"[^a-z0-9]", "", c)
        if qn == cn:
            return 95
        if qn in cn:
            return 80
        if q in c:
            return 75
        qt = set(q.split())
        ct = set(c.split())
        if qt and ct:
            overlap = len(qt & ct) / max(len(qt), 1)
            if overlap > 0.5:
                return int(overlap * 60)
        return 0

    @classmethod
    def _pick_best_match(cls, query: str, results: List[Dict]) -> Optional[Dict]:
        if not results:
            return None
        if len(results) == 1:
            return results[0]

        now_iso = datetime.utcnow().strftime("%Y-%m-%d")
        stale_cutoff = (
            datetime.utcnow().replace(year=datetime.utcnow().year - 1)
        ).strftime("%Y-%m-%d")

        def sort_key(m: Dict):
            score = cls._fuzzy(query, m.get("name", ""))
            rd = (m.get("releaseDateUtc") or "")[:10]
            is_upcoming = 1 if (rd and rd >= now_iso) else 0
            has_showtimes = 1 if m.get("hasScheduledShowtimes") else 0
            mid = int(m.get("id") or 0)
            return (score, is_upcoming, has_showtimes, rd, mid)

        results_sorted = sorted(results, key=sort_key, reverse=True)
        best = results_sorted[0]

        rd = (best.get("releaseDateUtc") or "")[:10]
        if rd and rd < stale_cutoff and not best.get("hasScheduledShowtimes"):
            log.info(
                "Skipping stale match for %r: id=%s name=%r release=%s "
                "(>1 yr old, no showtimes) — will re-check later",
                query, best.get("id"), best.get("name"), rd,
            )
            return None

        return best


    def get_theatre_by_slug(self, slug: str) -> Optional[Dict]:
        return self._request("GET", f"/v2/theatres/{slug}")

    def search_theatres(self, query: str, page_size: int = 20) -> List[Dict]:
        data = self._request(
            "GET", "/v2/theatres", {"name": query, "page-size": page_size}
        )
        if not data:
            return []
        return (data.get("_embedded") or {}).get("theatres") or []

    def resolve_theatre(self, query: str) -> Optional[Dict]:
        cached = self._theatre_cache.get(query)
        if cached is not None:
            return flatten_theatre_record(cached)

        t = self.get_theatre_by_slug(query)
        if t and t.get("name"):
            info = flatten_theatre_record(t)
            if info:
                info["slug"] = info.get("slug") or query
            self._theatre_cache.put(query, info)
            return info

        q = query.lower().replace(" ", "-")
        for suffix in range(1, 30):
            candidate = f"{q}-{suffix}"
            t = self.get_theatre_by_slug(candidate)
            if t and t.get("name"):
                info = flatten_theatre_record(t)
                if info:
                    info["slug"] = info.get("slug") or candidate
                self._theatre_cache.put(query, info)
                return info

        clean = query.replace("-", " ").replace("_", " ").strip()
        results = self.search_theatres(clean)
        if results:
            info = flatten_theatre_record(results[0])
            self._theatre_cache.put(query, info)
            return info

        log.warning("Could not resolve theatre: %r", query)
        return None


    def get_showtimes(
        self, theatre_id: int, date: str, movie_id: Optional[int] = None
    ) -> List[Dict]:
        params: Dict[str, Any] = {}
        if movie_id:
            params["movie-id"] = movie_id
        data = self._request(
            "GET", f"/v2/theatres/{theatre_id}/showtimes/{date}", params
        )
        if not data:
            return []
        return (data.get("_embedded") or {}).get("showtimes") or []


    def health_check(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "api_reachable": False,
            "key_valid": False,
            "error": None,
        }
        try:
            data = self._request("GET", "/v2/theatres/amc-lincoln-square-13")
            if data and data.get("name"):
                results["api_reachable"] = True
                results["key_valid"] = True
                results["sample_theatre"] = data.get("name")
            elif data is None:
                results["api_reachable"] = True
                results["error"] = "Theatre lookup returned 404"
        except AMCAuthError as e:
            results["api_reachable"] = True
            results["error"] = f"Auth rejected: {e}"
        except Exception as e:
            results["error"] = str(e)
        return results


class GraphQLClient:

    def __init__(self, timeout: int = 12, pool: int = 36):
        self.timeout = timeout if timeout and timeout > 0 else 12
        self._pool = pool
        backend, mod, self._impersonate = _gql_transport_backend()
        self._backend = backend
        if backend == "curl_cffi":
            self.s = mod.Session(impersonate=self._impersonate)
            log.info(
                "GraphQL transport: curl_cffi  impersonate=%s",
                self._impersonate,
            )
        else:
            log.warning(
                "install curl_cffi"
            )
            self.s = requests.Session()
            a = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=1)
            self.s.mount("https://", a)
        self.s.headers.update(build_gql_headers())
        self._warmed = False

    def refresh_headers(self):
        self.s.headers.clear()
        self.s.headers.update(build_gql_headers())

    def _post_gql(self, body: Any) -> Any:
        kwargs: Dict[str, Any] = {"json": body, "timeout": self.timeout}
        if self._backend == "curl_cffi":
            kwargs["impersonate"] = self._impersonate
        r = self.s.post(GRAPHQL, **kwargs)
        if r.status_code == 403:
            snippet = r.text[:500] if hasattr(r, "text") else "(no body)"
            if self._backend == "requests":
                log.error(
                    "GraphQL HTTP 403 — Cloudflare blocked plain Python TLS. "
                    "Install: pip install curl_cffi  |  body=%s", snippet,
                )
            else:
                log.error(
                    "GraphQL HTTP 403 (backend=%s, impersonate=%s). "
                    "body=%s", self._backend, self._impersonate, snippet,
                )
        r.raise_for_status()
        return r.json()

    def gql(self, query: str, variables: Optional[Dict] = None) -> Dict:
        body: Any = {"query": query}
        if variables:
            body["variables"] = variables
        data = self._post_gql(body)
        if isinstance(data, list):
            raise RuntimeError("Unexpected batch response for single operation")
        if data.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in data["errors"])
            log.error("GraphQL error: %s", msg)
            raise RuntimeError(msg)
        return data

    def gql_batch(self, ops: List[Dict]) -> List[Dict]:
        if not ops:
            return []
        payload = ops[0] if len(ops) == 1 else ops
        data = self._post_gql(payload)
        if isinstance(data, list):
            return data
        return [data]


    def get_movie(self, slug: str) -> Optional[Dict]:
        data = self.gql(
            """query($s:String!){viewer{movie(slug:$s){
            id movieId name runTime mpaaRating genre synopsis slug
            releaseDateUtc onlineTicketAvailabilityDateUtc status isReleased
            distributorCode preferredPoster{url}
        }}}""",
            {"s": slug},
        )
        m = data["data"]["viewer"]["movie"]
        return (
            m
            if m and m.get("name") and "not be found" not in (m.get("name") or "")
            else None
        )

    def resolve_movie_slug(self, query: str) -> Optional[str]:
        try:
            m = self.get_movie(query)
            if m:
                return m.get("slug")
        except Exception:
            pass
        q = query.lower().replace("-", " ").replace("_", " ")
        for src in (self.get_coming_soon, self.get_now_playing):
            try:
                for movie in src(200):
                    name = (movie.get("name") or "").lower()
                    slug = (movie.get("slug") or "").lower()
                    if q in name or q in slug or q.replace(" ", "-") in slug:
                        return movie["slug"]
            except Exception:
                pass
        return None

    def get_coming_soon(self, count: int = 200) -> List[Dict]:
        data = self.gql(
            f"""query{{viewer{{movies(availability:COMING_SOON,first:{count}){{
            edges{{node{{id movieId slug name status genre mpaaRating runTime
            releaseDateUtc onlineTicketAvailabilityDateUtc isReleased}}}}
        }}}}}}"""
        )
        return [e["node"] for e in data["data"]["viewer"]["movies"]["edges"]]

    def get_now_playing(self, count: int = 200) -> List[Dict]:
        data = self.gql(
            f"""query{{viewer{{movies(availability:NOW_PLAYING,first:{count}){{
            edges{{node{{id movieId slug name status genre mpaaRating runTime
            releaseDateUtc onlineTicketAvailabilityDateUtc}}}}
        }}}}}}"""
        )
        return [e["node"] for e in data["data"]["viewer"]["movies"]["edges"]]

    def get_theatre(self, slug: str) -> Optional[Dict]:
        try:
            data = self.gql(
                """query($s:String!){viewer{theatre(slug:$s){
                id theatreId name slug addressLine1 city stateCode postalCode
                attributes{edges{node{name description}}}
            }}}""",
                {"s": slug},
            )
            return data["data"]["viewer"]["theatre"]
        except Exception:
            return None

    def resolve_theatre_slug(self, query: str) -> Optional[str]:
        t = self.get_theatre(query)
        if t and t.get("name"):
            return query
        q = query.lower().replace(" ", "-")
        consecutive_misses = 0
        for suffix in range(1, 30):
            candidate = f"{q}-{suffix}"
            t = self.get_theatre(candidate)
            if t and t.get("name"):
                return candidate
            consecutive_misses += 1
            if consecutive_misses >= 3:
                break
        return None

    def get_theatre_showtimes(self, theatre_slug: str, date: str) -> List[Dict]:
        data = self.gql(
            """query($s:String!,$d:Date!){viewer{user{id
            movies(theatreSlug:$s,date:$d){items{theatres{theatre{name theatreId}
            formats{items{id groups(first:20){edges{node{
            showtimes(first:100){edges{node{
                id showtimeId showDateTimeUtc status
                display{time amPm date prefix}
                movie{id movieId name slug}
        }}}}}}}}}}}}}}""",
            {"s": theatre_slug, "d": date},
        )
        out = []
        for item in data["data"]["viewer"]["user"]["movies"]["items"]:
            for th in item.get("theatres") or []:
                for fmt in (th.get("formats") or {}).get("items") or []:
                    for grp in (fmt.get("groups") or {}).get("edges") or []:
                        for st in (grp["node"].get("showtimes") or {}).get("edges") or []:
                            out.append(st["node"])
        return out

    def get_showtime_attrs_batch(self, sids: List[int]) -> Dict[int, Dict]:
        if not sids:
            return {}
        vd = [f"$s{i}:Int!" for i in range(len(sids))]
        ps = [
            f"st{i}:showtime(id:$s{i}){{showtimeId status auditorium isDiscountDaysEligible "
            f"attributes(first:16){{edges{{node{{code name description}}}}}} seatingLayout{{rows columns}}}}"
            for i in range(len(sids))
        ]
        q = f"query({','.join(vd)}){{viewer{{{' '.join(ps)}}}}}"
        data = self.gql(q, {f"s{i}": sid for i, sid in enumerate(sids)})
        v = data["data"]["viewer"]
        return {
            sid: v[f"st{i}"]
            for i, sid in enumerate(sids)
            if v.get(f"st{i}") and v[f"st{i}"].get("showtimeId")
        }


    def get_seat_layout(self, sid: int) -> Dict:
        data = self.gql(
            """query($sid:Int!){viewer{showtime(id:$sid){
            id showtimeId showDateTimeUtc status
            seatingLayout{id columns rows isZoned
                seats{id column row available seatTier shouldDisplay type name}}
        }}}""",
            {"sid": sid},
        )
        return data["data"]["viewer"]["showtime"]


    def _warm_session(self):
        if self._warmed:
            return
        try:
            self.gql("{viewer{user{id}}}")
            self._warmed = True
        except Exception as e:
            log.debug("Session warm-up query failed (non-fatal): %s", e)

    def create_order(self, sid: int, seats: List[Tuple[int, int]]) -> Dict:
        self._warm_session()
        products = [
            {"sku": f"TICKET-RS-{sid}-ADULT", "quantity": 1, "row": r, "column": c}
            for r, c in seats
        ]
        data = self.gql(
            """mutation orderCreate($input:OrderCreateInput!){
            orderCreate(input:$input){order{id token status total displayTotal expirationDateUtc}}
        }""",
            {"input": {"products": products, "waiveSubscriptionDiscounts": False}},
        )
        return data["data"]["orderCreate"]["order"]

    def create_order_batch(
        self, orders: List[Tuple[int, List[Tuple[int, int]]]]
    ) -> List[Dict]:
        self._warm_session()
        ops = []
        for sid, coords in orders:
            products = [
                {"sku": f"TICKET-RS-{sid}-ADULT", "quantity": 1, "row": r, "column": c}
                for r, c in coords
            ]
            ops.append(
                {
                    "query": (
                        "mutation($i:OrderCreateInput!)"
                        "{orderCreate(input:$i){order{token status displayTotal expirationDateUtc}}}"
                    ),
                    "variables": {
                        "i": {"products": products, "waiveSubscriptionDiscounts": False}
                    },
                }
            )
        results_raw = self.gql_batch(ops)
        results: List[Dict] = []
        for d in results_raw:
            try:
                o = d["data"]["orderCreate"]["order"]
                if o and o.get("token"):
                    results.append(
                        {
                            "token": o["token"],
                            "total": o.get("displayTotal"),
                            "expires": o.get("expirationDateUtc"),
                            "error": None,
                            "error_code": 0,
                        }
                    )
                    continue
            except (KeyError, TypeError):
                pass
            ec, em = 0, "unknown"
            try:
                inner = d["errors"][0]["extensions"]["exception"]["originalError"][
                    "error"
                ]["errors"][0]
                ec = inner.get("code", 0)
                em = inner.get("exceptionMessage", d["errors"][0]["message"])
            except Exception:
                try:
                    em = d["errors"][0]["message"]
                except Exception:
                    pass
            results.append({"token": None, "error": em, "error_code": ec})
        return results

    def cancel_order(self, token: str) -> bool:
        try:
            self.gql(
                'mutation($t:String!){orderDelete(input:{token:$t}){success}}',
                {"t": token},
            )
            return True
        except Exception as e:
            log.warning("Cancel order %s failed: %s", token[:8], e)
            return False

    def extend_order(self, token: str) -> Optional[str]:
        try:
            data = self.gql(
                "mutation($t:String!){orderExpirationUpdate(input:{token:$t}){order{expirationDateUtc}}}",
                {"t": token},
            )
            return data["data"]["orderExpirationUpdate"]["order"][
                "expirationDateUtc"
            ]
        except Exception as e:
            log.warning("Extend order %s failed: %s", token[:8], e)
            return None


class AMCClient:

    def __init__(
        self, vendor_key: str = "", timeout: int = 12, pool: int = 36
    ):
        self.rest = OfficialAMCClient(
            vendor_key=vendor_key, timeout=timeout, pool=pool
        )
        self.gql = GraphQLClient(timeout=timeout, pool=pool)

    def resolve_movie(self, q: str) -> Optional[Dict]:
        return self._resolve_movie_dual(q)

    def resolve_theatre(self, q: str) -> Optional[Dict]:
        return self._resolve_theatre_dual(q)

    def search_movies(self, q: str, **kw) -> List[Dict]:
        return self.rest.search_movies(q, **kw)

    def get_movie_by_id(self, mid: int) -> Optional[Dict]:
        return self.rest.get_movie_by_id(mid)

    def get_theatre_by_slug(self, s: str) -> Optional[Dict]:
        return self.rest.get_theatre_by_slug(s)

    def search_theatres(self, q: str, **kw) -> List[Dict]:
        return self.rest.search_theatres(q, **kw)

    def get_showtimes(self, tid: int, date: str, **kw) -> List[Dict]:
        return self._get_showtimes_dual(
            tid, date, kw.get("movie_id"), kw.get("theatre_slug")
        )

    def health_check(self) -> Dict[str, Any]:
        return self.rest.health_check()

    def get_seat_layout(self, sid: int) -> Dict:
        return self.gql.get_seat_layout(sid)

    def create_order(self, sid: int, seats: List[Tuple[int, int]]) -> Dict:
        return self.gql.create_order(sid, seats)

    def create_order_batch(
        self, orders: List[Tuple[int, List[Tuple[int, int]]]]
    ) -> List[Dict]:
        return self.gql.create_order_batch(orders)

    def cancel_order(self, token: str) -> bool:
        return self.gql.cancel_order(token)

    def extend_order(self, token: str) -> Optional[str]:
        return self.gql.extend_order(token)

    def resolve_movie_slug(self, query: str) -> Optional[str]:
        m = self._resolve_movie_dual(query)
        return m["slug"] if m else None

    def resolve_theatre_slug(self, query: str) -> Optional[str]:
        t = self._resolve_theatre_dual(query)
        return t["slug"] if t else None

    def get_movie(self, slug_or_id: str) -> Optional[Dict]:
        m = self._resolve_movie_dual(slug_or_id)
        if m:
            return self.rest.get_movie_by_id(m["id"])
        return None

    def get_theatre(self, slug: str) -> Optional[Dict]:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fs = {
                ex.submit(self.rest.get_theatre_by_slug, slug): "rest",
                ex.submit(self.gql.get_theatre, slug): "gql",
            }
            first = None
            results = {}
            for f in as_completed(fs):
                src = fs[f]
                try:
                    val = f.result()
                    results[src] = val
                    if val and first is None:
                        first = val
                except Exception as e:
                    log.debug("Theatre %s failed: %s", src, e)
            if results.get("rest") and results.get("gql"):
                rn, gn = results["rest"].get("name"), results["gql"].get("name")
                if rn and gn and rn != gn:
                    log.warning("Theatre discrepancy %s: REST=%r GQL=%r", slug, rn, gn)
            return first

    def get_poster(self, query: str) -> str:
        m = self._resolve_movie_dual(query)
        return m["poster_url"] if m else ""

    def get_coming_soon(self, count: int = 200) -> List[Dict]:
        out = []
        try:
            out.extend(self.gql.get_coming_soon(count))
        except Exception as e:
            log.debug("GQL coming soon failed: %s", e)
        return out

    def get_now_playing(self, count: int = 200) -> List[Dict]:
        try:
            return self.gql.get_now_playing(count)
        except Exception:
            return []

    def get_theatre_showtimes(self, theatre_slug: str, date: str) -> List[Dict]:
        t = self._resolve_theatre_dual(theatre_slug)
        if not t:
            return []
        movie_id = None
        return self._get_showtimes_dual(int(t["id"]), date, movie_id)

    def get_showtime_attrs_batch(self, sids: List[int]) -> Dict[int, Dict]:
        try:
            return self.gql.get_showtime_attrs_batch(sids)
        except Exception:
            return {}

    def _resolve_movie_dual(self, q: str) -> Optional[Dict]:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fs = {
                ex.submit(self.rest.resolve_movie, q): "rest",
                ex.submit(self._resolve_movie_via_gql, q): "gql",
            }
            first = None
            results = {}
            for f in as_completed(fs):
                src = fs[f]
                try:
                    val = f.result()
                    results[src] = val
                    if val and first is None:
                        first = val
                except Exception as e:
                    log.debug("Movie resolve %s failed: %s", src, e)
            if results.get("rest") and results.get("gql"):
                if results["rest"].get("id") != results["gql"].get("id"):
                    log.warning(
                        "Movie resolve discrepancy %r: REST=%s GQL=%s",
                        q,
                        results["rest"].get("id"),
                        results["gql"].get("id"),
                    )
            return first

    def _resolve_movie_via_gql(self, q: str) -> Optional[Dict]:
        slug = self.gql.resolve_movie_slug(q)
        if not slug:
            return None
        m = self.gql.get_movie(slug)
        if not m:
            return None
        return {
            "id": m.get("movieId") or m.get("id"),
            "name": m.get("name", ""),
            "slug": m.get("slug", ""),
            "poster_url": (m.get("preferredPoster") or {}).get("url", ""),
            "release_date": m.get("releaseDateUtc", ""),
            "ticket_avail_date": m.get("onlineTicketAvailabilityDateUtc", ""),
            "status": m.get("status", ""),
            "mpaa_rating": m.get("mpaaRating", ""),
            "genre": m.get("genre", ""),
            "run_time": m.get("runTime", 0),
        }

    def _resolve_theatre_dual(self, q: str) -> Optional[Dict]:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fs = {
                ex.submit(self.rest.resolve_theatre, q): "rest",
                ex.submit(self._resolve_theatre_via_gql, q): "gql",
            }
            first = None
            results = {}
            for f in as_completed(fs):
                src = fs[f]
                try:
                    val = f.result()
                    results[src] = val
                    if val and first is None:
                        first = val
                except Exception as e:
                    log.debug("Theatre resolve %s failed: %s", src, e)
            merged = merge_theatre_records(results.get("rest"), results.get("gql"))
            if merged:
                rs, gs = (results.get("rest") or {}).get("slug"), (
                    results.get("gql") or {}
                ).get("slug")
                if rs and gs and rs != gs:
                    log.warning("Theatre resolve discrepancy %r: REST=%s GQL=%s", q, rs, gs)
                return merged
            return first

    def _resolve_theatre_via_gql(self, q: str) -> Optional[Dict]:
        slug = self.gql.resolve_theatre_slug(q)
        if not slug:
            return None
        t = self.gql.get_theatre(slug)
        if not t:
            return None
        return flatten_theatre_record({**t, "slug": slug or (t.get("slug") or "")})

    def _get_showtimes_dual(
        self,
        theatre_id: int,
        date: str,
        movie_id: Optional[int],
        theatre_slug: Optional[str] = None,
    ) -> List[Dict]:
        out_rest: List[Dict] = []
        out_gql: List[Dict] = []
        slug = (theatre_slug or "").strip() or None
        if not slug and theatre_id:
            slug = self._theatre_slug_from_id(theatre_id)
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                tasks = {ex.submit(self.rest.get_showtimes, theatre_id, date, movie_id): "rest"}
                if slug:
                    tasks[ex.submit(self.gql.get_theatre_showtimes, slug, date)] = "gql"
                for f in as_completed(tasks):
                    src = tasks[f]
                    try:
                        val = f.result() or []
                        if src == "rest":
                            out_rest = val
                        else:
                            out_gql = val
                    except Exception as e:
                        log.debug("Showtimes %s failed: %s", src, e)
        except Exception as e:
            log.debug("Showtime dual wrapper failed: %s", e)

        gql_filtered = self._normalize_gql_showtimes(out_gql, theatre_id, date, movie_id) if out_gql else []

        if out_rest and gql_filtered:
            rest_sids = {s.get("id") for s in out_rest}
            gql_sids = {s.get("id") for s in gql_filtered}
            only_rest = rest_sids - gql_sids
            only_gql = gql_sids - rest_sids
            if only_rest or only_gql:
                log.info(
                    "Showtime cross-check theatre=%s date=%s movie=%s: "
                    "REST=%d GQL=%d overlap=%d rest-only=%d gql-only=%d",
                    theatre_id, date, movie_id,
                    len(rest_sids), len(gql_sids), len(rest_sids & gql_sids),
                    len(only_rest), len(only_gql),
                )

        return out_rest or gql_filtered

    def _normalize_gql_showtimes(
        self, raw: List[Dict], theatre_id: int, date: str, movie_id: Optional[int]
    ) -> List[Dict]:
        out = []
        for s in raw:
            mov = s.get("movie") or {}
            if movie_id and str(mov.get("movieId")) != str(movie_id):
                continue
            local_time = s.get("display", {}).get("time", "")
            ampm = s.get("display", {}).get("amPm", "")
            dt_local = f"{date}T00:00:00"
            out.append(
                {
                    "id": s.get("showtimeId") or s.get("id"),
                    "performanceNumber": s.get("showtimeId") or s.get("id"),
                    "movieId": mov.get("movieId"),
                    "movieName": mov.get("name", ""),
                    "showDateTimeUtc": s.get("showDateTimeUtc", ""),
                    "showDateTimeLocal": dt_local,
                    "isSoldOut": False,
                    "isAlmostSoldOut": False,
                    "isCanceled": (s.get("status") or "").upper() == "CANCELED",
                    "theatreId": theatre_id,
                    "auditorium": 0,
                    "premiumFormat": "",
                    "isDiscountDaysEligible": None,
                    "attributes": [],
                    "_gql_display": f"{local_time} {ampm}".strip(),
                }
            )
        return out

    def _theatre_slug_from_id(self, theatre_id: int) -> Optional[str]:
        for key in list(self.rest._theatre_cache._data.keys()):
            v = self.rest._theatre_cache.get(key)
            if v and str(v.get("id")) == str(theatre_id):
                return v.get("slug")
        return None
