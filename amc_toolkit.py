from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback

from amc_api import (
    AMCClient,
    Showtime,
    parse_seats,
    find_groups,
    non_overlapping,
    tier_label,
    matches_format,
    matches_seating,
    matches_subtitles,
)

log = logging.getLogger("amc.toolkit")


def _mc() -> AMCClient:
    return AMCClient(timeout=12)


def cmd_seats(args):
    client = _mc()
    sid = args.showtime_id
    try:
        raw = client.get_seat_layout(sid)
    except Exception as e:
        print(f"  Failed to fetch seat layout: {e}")
        return
    sl = (raw or {}).get("seatingLayout")
    if not sl:
        print("  No layout found for this showtime.")
        return
    seats, rows, cols = parse_seats(sl)
    thresh = args.min_score
    good = [s for s in seats if s.score >= thresh]
    groups = find_groups(sid, seats, rows, cols, args.group)
    gg = [g for g in groups if g.score >= thresh]
    print(f"\n{'═' * 70}")
    print(f"  SID {sid} — {rows}x{cols} — {len(seats)} open")
    print(f"  {len(good)} scoring {thresh}+ | {len(gg)} groups of {args.group}+")
    print(f"{'═' * 70}\n")
    grid = {(s["row"], s["column"]): s for s in sl.get("seats", [])}
    sm = {s.name: s for s in seats}
    print(f"{'SCREEN':^{cols * 3}}")
    print(f"  {'─' * (cols * 3)}")
    for r in range(1, rows + 1):
        line = f"  {r:>2} "
        for c in range(1, cols + 1):
            cell = grid.get((r, c))
            if not cell or not cell.get("shouldDisplay"):
                line += "   "
                continue
            sc = sm.get(cell.get("name", ""))
            if not cell.get("available"):
                line += " ❌"
            elif sc and sc.score >= 80:
                line += " 🟢"
            elif sc and sc.score >= 70:
                line += " 🔵"
            elif sc and sc.score >= 60:
                line += " 🟡"
            elif sc and sc.score >= 55:
                line += " 🟠"
            else:
                line += " ⚪"
        print(line)
    print(f"  🟢80+ 🔵70+ 🟡60+ 🟠55+ ⚪<55 ❌taken\n")
    if gg:
        print("  Top groups:")
        for g in gg[:10]:
            url = f"https://www.amctheatres.com/showtimes/{sid}/tickets?seats={g.query}"
            print(f"  {g.names:>15} {g.score:>5.1f} {g.tier:>12}  {url}")
    print()


def cmd_discover(args):
    client = _mc()
    from amc_monitor import discover_showtimes_legacy, Cfg, resolve_config

    cfg = Cfg(
        movie=args.movie,
        theatre=args.theatre,
        format=args.format,
        seating=args.seating,
        subtitles=args.subtitles,
        scan_days=args.days,
        workers=args.workers,
    )
    try:
        resolve_config(cfg, client)
    except Exception as e:
        print(f"  Resolution failed: {e}")
        return
    showtimes = discover_showtimes_legacy(cfg, client)
    if not showtimes:
        print("  No showtimes found.")
        return
    for st in showtimes:
        disc = "💰" if st.discount_eligible and not st.discount_excluded else ("🚫" if st.discount_excluded else "  ")
        sold = "⚠️" if st.is_almost_sold_out else ("❌" if st.is_sold_out else "  ")
        price = st.price_label or ""
        print(f"  {st.date} {st.time:>5} {st.ampm:<2} {disc}{sold} SID {st.sid:>10} {st.status:<12} {st.format_label:<14} {price}")
    print(f"\n  {len(showtimes)} showtimes found.\n")


def cmd_status(args):
    from amc_monitor import Cfg, status_dashboard

    cfg = Cfg(
        movie=args.movie,
        theatre=args.theatre,
        format=args.format,
        scan_days=args.days,
        quantity=args.group,
        tier="ALL",
    )
    status_dashboard(cfg)


def cmd_movie(args):
    client = _mc()
    try:
        m = client.resolve_movie(args.name)
    except Exception as e:
        print(f"  Resolution failed: {e}")
        return
    if not m:
        print(f"  Not found: {args.name}")
        return

    mid = m.get("id", 0)
    full = client.get_movie_by_id(mid) if mid else None
    if full:
        print(f"\n  {full.get('name', '?')}")
        print(f"  Slug:    {full.get('slug', '?')}")
        print(f"  ID:      {full.get('id', '?')}")
        print(f"  Status:  {full.get('status', '?')}")
        print(f"  Rating:  {full.get('mpaaRating') or '?'}")
        print(f"  Runtime: {full.get('runTime', '?')} min")
        print(f"  Genre:   {full.get('genre') or '?'}")
        print(f"  Release: {full.get('releaseDateUtc', '?')}")
        print(f"  On-Sale: {full.get('onlineTicketAvailabilityDateUtc') or 'Not set'}")
        poster = (full.get("media") or {}).get("posterDynamic") or (full.get("preferredPoster") or {}).get("url", "—")
        print(f"  Poster:  {poster}")
        if full.get("hasScheduledShowtimes"):
            print(f"  Showtimes: Available")
    else:
        print(f"\n  {m.get('name', '?')}")
        print(f"  ID:      {m.get('id', '?')}")
        print(f"  Slug:    {m.get('slug', '?')}")
        print(f"  Status:  {m.get('status', '?')}")
        print(f"  Poster:  {m.get('poster_url', '—')}")
    print()


def cmd_theatre(args):
    client = _mc()
    try:
        t = client.resolve_theatre(args.name)
    except Exception as e:
        print(f"  Resolution failed: {e}")
        return
    if not t:
        print(f"  Not found: {args.name}")
        return

    slug = t.get("slug", args.name)
    full = client.get_theatre(slug)
    if full and full.get("name"):
        name = full.get("name", "?")
        tid = full.get("theatreId") or full.get("id") or t.get("id", "?")
        addr = full.get("addressLine1", "")
        city = full.get("city", "")
        state_code = full.get("stateCode", "")
        postal = full.get("postalCode", "")

        attrs: list[str] = []
        raw_attrs = full.get("attributes")
        if isinstance(raw_attrs, dict):
            attrs = [e.get("node", {}).get("name", "") for e in (raw_attrs.get("edges") or []) if e.get("node", {}).get("name")]
        elif isinstance(raw_attrs, list):
            attrs = [a.get("name", "") for a in raw_attrs if isinstance(a, dict) and a.get("name")]

        print(f"\n  {name} (ID: {tid})")
        if addr:
            print(f"  {addr}, {city}, {state_code} {postal}")
        if attrs:
            print(f"  Formats: {', '.join(attrs)}")
    else:
        print(f"\n  {t.get('name', '?')} (ID: {t.get('id', '?')})")
        print(f"  Slug: {t.get('slug', '?')}")
    print()


def cmd_reserve(args):
    client = _mc()
    sid = args.showtime_id
    names = [s.upper() for s in args.seats]
    try:
        raw = client.get_seat_layout(sid)
    except Exception as e:
        print(f"  Failed to fetch layout: {e}")
        return
    sl = (raw or {}).get("seatingLayout")
    if not sl:
        print("  No layout")
        return
    sm = {
        s.get("name", "").upper(): (s["row"], s["column"])
        for s in sl.get("seats", [])
        if s.get("name")
    }
    coords = []
    for n in names:
        if n not in sm:
            print(f"  Seat {n} not found")
            return
        coords.append(sm[n])
    try:
        o = client.create_order(sid, coords)
        t = o.get("token")
        print(f"\n  RESERVED!")
        print(f"  Token:   {t}")
        print(f"  Total:   {o.get('displayTotal')}")
        print(f"  Expires: {o.get('expirationDateUtc')}")
        print(f"  URL:     https://www.amctheatres.com/orders/{t}/food-and-drink\n")
    except Exception as e:
        print(f"  Failed: {e}")


def cmd_cancel(args):
    ok = _mc().cancel_order(args.token)
    print("  Cancelled" if ok else "  Failed")


def cmd_coming(args):
    client = _mc()
    movies = []
    try:
        movies = client.get_coming_soon(200)
    except Exception as e:
        log.debug("GQL coming soon failed: %s", e)
    if not movies:
        try:
            movies = client.search_movies("coming soon", page_size=50)
        except Exception as e:
            print(f"  Failed: {e}")
            return
    for m in sorted(movies, key=lambda x: x.get("releaseDateUtc", "9999")):
        name = (m.get("name") or "?")[:49]
        release = m.get("releaseDateUtc", "?")[:12]
        status = m.get("status", "?")
        print(f"  {name:50s} {release:12s} {status}")
    if not movies:
        print("  No movies found.")


def cmd_playing(args):
    client = _mc()
    movies = []
    try:
        movies = client.get_now_playing(200)
    except Exception as e:
        log.debug("GQL now playing failed: %s", e)
    for m in movies:
        name = (m.get("name") or "?")[:49]
        genre = (m.get("genre") or "")[:20]
        rating = m.get("mpaaRating") or ""
        print(f"  {name:50s} {genre:20s} {rating}")
    if not movies:
        print("  No movies found.")


def cmd_showtimes(args):
    client = _mc()
    try:
        t = client.resolve_theatre(args.theatre)
    except Exception as e:
        print(f"  Theatre resolution failed: {e}")
        return
    if not t:
        print(f"  Theatre not found: {args.theatre}")
        return

    tid = int(t.get("id", 0))
    if not tid:
        print(f"  No theatre ID for {args.theatre}")
        return

    sts = client.get_showtimes(tid, args.date)
    if not sts:
        print(f"  No showtimes for {args.date}")
        return

    for raw in sts:
        sid = raw.get("id", 0)
        name = raw.get("movieName", "?")[:30]
        dt_local = raw.get("showDateTimeLocal", "?")
        attrs = raw.get("attributes") or []
        codes = [a.get("code", "") for a in attrs]
        names = [a.get("name", "") for a in attrs]
        disc = raw.get("isDiscountDaysEligible")
        sold = "SOLD" if raw.get("isSoldOut") else ("ALMOST" if raw.get("isAlmostSoldOut") else "")
        prices = raw.get("ticketPrices") or []
        price_str = ", ".join(f"{p.get('type','?')}=${p.get('price',0):.2f}" for p in prices[:3])
        print(f"  SID {sid:>10} | {dt_local:>20} | {name:30s} | {sold:6s} | disc={disc}")
        if codes:
            print(f"    codes: {', '.join(codes)}")
        if names:
            print(f"    attrs: {', '.join(names)}")
        if price_str:
            print(f"    prices: {price_str}")
    print(f"\n  {len(sts)} showtimes.\n")


def cmd_health(args):
    client = _mc()
    h = client.health_check()
    print(f"\n  API Health Check")
    print(f"  Reachable:    {'Yes' if h.get('api_reachable') else 'No'}")
    print(f"  Key Valid:    {'Yes' if h.get('key_valid') else 'No'}")
    if h.get("sample_theatre"):
        print(f"  Sample:       {h['sample_theatre']}")
    if h.get("error"):
        print(f"  Error:        {h['error']}")
    print()


def main():
    p = argparse.ArgumentParser(description="AMC+ Toolkit v8.1")
    p.add_argument("--log-level", default="WARNING")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("seats", help="Show seat map for a showtime")
    s.add_argument("showtime_id", type=int)
    s.add_argument("--group", type=int, default=2)
    s.add_argument("--min-score", type=float, default=55)

    s = sub.add_parser("discover", help="Discover showtimes for a movie")
    s.add_argument("movie")
    s.add_argument("--theatre", default="amc-lincoln-square-13")
    s.add_argument("--format", default="")
    s.add_argument("--seating", default="")
    s.add_argument("--subtitles", default="")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--workers", type=int, default=32)

    s = sub.add_parser("status", help="Full status dashboard for a movie")
    s.add_argument("movie")
    s.add_argument("--theatre", default="amc-lincoln-square-13")
    s.add_argument("--format", default="")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--group", type=int, default=2)

    s = sub.add_parser("movie", help="Look up movie info")
    s.add_argument("name")

    s = sub.add_parser("theatre", help="Look up theatre info")
    s.add_argument("name")

    s = sub.add_parser("reserve", help="Reserve specific seats")
    s.add_argument("showtime_id", type=int)
    s.add_argument("seats", nargs="+")

    s = sub.add_parser("cancel", help="Cancel a held order")
    s.add_argument("token")

    sub.add_parser("coming", help="List coming soon movies")
    sub.add_parser("playing", help="List now playing movies")

    s = sub.add_parser("showtimes", help="Raw showtime dump for a theatre + date")
    s.add_argument("theatre")
    s.add_argument("date", help="YYYY-MM-DD")

    sub.add_parser("health", help="API health check")

    args = p.parse_args()
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
    )
    if not args.cmd:
        p.print_help()
        return

    dispatch = {
        "seats": cmd_seats,
        "discover": cmd_discover,
        "status": cmd_status,
        "movie": cmd_movie,
        "theatre": cmd_theatre,
        "reserve": cmd_reserve,
        "cancel": cmd_cancel,
        "coming": cmd_coming,
        "playing": cmd_playing,
        "showtimes": cmd_showtimes,
        "health": cmd_health,
    }
    try:
        dispatch[args.cmd](args)
    except Exception as e:
        print(f"  Error: {e}")
        if args.log_level.upper() == "DEBUG":
            traceback.print_exc()


if __name__ == "__main__":
    main()
