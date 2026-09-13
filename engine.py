#!/usr/bin/env python3
"""
Scanning engine for the FFXIV Materia <-> NA arbitrage tool.

Both directions are modelled the same way, and both are asymmetric in the same
manner: you can buy from anywhere you can travel to, but you can only sell on the
one world your character calls home, because market boards are per-world.

  NA -> Materia   buy anywhere in North America, sell on one Materia world
  Materia -> NA   buy anywhere on Materia,       sell on one NA world

So the buy side is always a region-wide search for the cheapest source, and the
sell side is always priced per world against that world's own board and demand.

This module does the work; app.py is the window around it. It can also be run
directly for a headless refresh:  python engine.py

No third-party packages. Python 3.9+.
"""

import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import paths

# ----------------------------------------------------------------------------
# Config -- edit these to change what the scan looks for.
# ----------------------------------------------------------------------------

OCE_DC = "Materia"            # the Oceanian data center
NA_REGION = "North-America"   # region name as Universalis spells it
NA_PROBE_DC = "Aether"        # any NA DC; its "region" aggregates cover all of NA

TAX = 0.95                    # seller receives 95% after the 5% market board tax
BIG_TICKET_FLOOR = 20_000     # gil/unit dividing the two NA -> Materia tiers

MIN_MARGIN = 0.12             # 12% -- below this the trip isn't worth the slot
MAX_MARGIN = 15.0             # 1500% -- above this it's a private trade, not demand
MIN_DC_SALES = 4              # sales in the window before we'll judge demand at all
MAX_SALE_AGE_DAYS = 7         # the item must have sold on the sell side recently
PRICE_SANITY = 1.5            # cap the sell price at 1.5x the DC median sale
NEAR_MIN = 1.25               # "cheap" stock = within 25% of the buy floor
OUTLIER_FLOOR = 0.25          # ignore listings under a quarter of the median

# Universalis enforces a hard cap of 8 concurrent connections per client and
# answers the 9th with "429 Too Many Requests: max connections reached: 8".
# This is a server limit, not a tuning knob -- going higher just generates
# rejections and retry backoff, which makes a scan slower, not faster. To go
# faster, ask for more items per request (BATCH below), not more connections.
WORKERS = 8

# Items per request. The API takes a comma-separated list, and asking for
# several at once is the only way to go faster once connections are capped.
# Measured at 8 workers: 1 item/request gave 6.1 items/s, 4 gave 9.7, 6 gave
# 13.6, 8 gave 10.2 with errors. Past about 20 the server spends so long
# building the response that it returns 504, so 6 is the sweet spot with room
# to spare.
BATCH = 6

CANDIDATES_PER_TIER = 200     # items deep-fetched per tier

# Sweeping one world for your own listings is a different shape of job from a
# scan: no sale history is needed, and a single world's board is a fraction of
# a data centre's. That makes far bigger batches safe. Measured on a random
# 600-item sample: 40 per request gave 375 items/s, 80 gave 525, both clean --
# so the whole 16,845-item catalogue on one world lands in well under a minute.
FIND_BATCH = 50

SNAPSHOT_VERSION = 2          # bumped when the cache layout changes

CACHE = paths.DATA_DIR
SNAPSHOT = os.path.join(CACHE, "snapshot.json")
HISTORY = os.path.join(CACHE, "history.json")
NAMES = os.path.join(CACHE, "names.json")
HISTORY_KEEP = 8          # scans retained for trend arrows
UA = {"User-Agent": "ffxiv-arb/1.0 (personal market board tool)"}

# The only endpoints this tool is allowed to reach, and only over TLS.
ALLOWED_PREFIXES = ("https://universalis.app/api/", "https://v2.xivapi.com/api/")
MAX_RESPONSE = 32 * 1024 * 1024   # refuse absurd payloads rather than eat all RAM

# The three tables the app shows. Each names its buy side and its sell side.
TIERS = {
    "big":  {"label": "Big ticket",          "buy": "na",  "sell": "oce"},
    "bulk": {"label": "Bulk & consumables",  "buy": "na",  "sell": "oce"},
    "rev":  {"label": "Materia → NA",   "buy": "oce", "sell": "na"},
}


class Cancelled(Exception):
    """Raised when the caller asks for an in-flight scan to stop."""


class Unreachable(RuntimeError):
    """Universalis (or the network) isn't answering."""


def health(timeout=10):
    """Quick probe so the app can say *why* it has no fresh data.

    Returns (ok, message). Distinguishes a dead connection from a dead API,
    because the fix is different and the user deserves to know which it is.
    """
    url = "https://universalis.app/api/v2/worlds"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read(4 * 1024 * 1024).decode())
        if isinstance(body, list) and body:
            return True, "Universalis is up."
        return False, "Universalis answered, but with nothing usable."
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:
            return False, f"Universalis is having problems (HTTP {e.code})."
        return False, f"Universalis refused the request (HTTP {e.code})."
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, OSError) and getattr(reason, "errno", None) == 11001:
            return False, "No internet connection."
        return False, f"Can't reach Universalis: {reason}"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"Can't reach Universalis: {e}"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Fetcher:
    def __init__(self, report, should_stop):
        self.report = report
        self.should_stop = should_stop
        self.fails = 0
        self.calls = 0
        self.splits = 0        # oversized requests the caller retried smaller
        self.cancelled = False

    @property
    def degraded(self):
        """True when enough requests failed that the results have holes in them."""
        return self.calls > 20 and self.fails / self.calls > 0.1

    def check(self):
        if self.should_stop():
            raise Cancelled()

    def fetch(self, url, tries=4):
        """Return (payload, status). status is the HTTP code, or None locally.

        Callers that can react to a specific failure -- a gateway timeout means
        "you asked for too much at once" -- use this; everything else uses get().
        """
        # Only ever talk to the two APIs this tool is built against, over TLS.
        # A redirect that tries to move us off HTTPS or onto another host is
        # treated as a failure rather than followed.
        if not any(url.startswith(p) for p in ALLOWED_PREFIXES):
            raise ValueError(f"Refusing to fetch an unexpected URL: {url[:80]}")
        self.calls += 1
        status = None
        for attempt in range(tries):
            self.check()
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=90) as r:
                    if not any(r.geturl().startswith(p) for p in ALLOWED_PREFIXES):
                        raise urllib.error.URLError(
                            f"redirected off-site to {r.geturl()[:60]}")
                    if int(r.headers.get("Content-Length") or 0) > MAX_RESPONSE:
                        raise urllib.error.URLError("response larger than expected")
                    body = r.read(MAX_RESPONSE + 1)
                    if len(body) > MAX_RESPONSE:
                        raise urllib.error.URLError("response larger than expected")
                    payload = json.loads(body.decode())
                    # The API is third-party: never assume the shape we wanted.
                    if not isinstance(payload, (dict, list)):
                        raise json.JSONDecodeError("unexpected JSON shape", "", 0)
                    return payload, 200
            except urllib.error.HTTPError as e:
                status = e.code
                if e.code in (502, 503, 504):
                    # The server gave up building this response. Retrying it
                    # unchanged will usually fail the same way, so hand it back
                    # for the caller to split. Not counted as a failure here:
                    # only the caller knows whether the retry recovered it.
                    self.splits += 1
                    return None, status
                if e.code == 429:
                    # Too many open connections. Wait longer than usual before
                    # trying again, rather than adding to the pile.
                    if attempt == tries - 1:
                        self.fails += 1
                        return None, status
                    time.sleep(2.0 * (attempt + 1))
                    continue
            except (urllib.error.URLError, OSError, json.JSONDecodeError,
                    UnicodeDecodeError):
                pass
            if attempt == tries - 1:
                self.fails += 1
                return None, status
            time.sleep(1.2 * (attempt + 1))
        return None, status

    def get(self, url, tries=4):
        return self.fetch(url, tries)[0]

    def phase(self, start, end):
        """Set which slice of the overall scan the next map() call covers."""
        self.span = (start, end)

    def map(self, fn, items, label):
        out, done, total = [], 0, len(items)
        start, end = getattr(self, "span", (0.0, 1.0))
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            try:
                for res in ex.map(fn, items):
                    done += 1
                    if done % 10 == 0 or done == total:
                        overall = start + (end - start) * (done / total)
                        self.report(f"{label} {done}/{total}", overall)
                    out.append(res)
            except Cancelled:
                # Keep whatever arrived. The caller decides whether a partial
                # result is worth showing.
                self.cancelled = True
                ex.shutdown(wait=False, cancel_futures=True)
        return out


def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
        if obj is None:
            return None
    return obj


def clean(entries, hq, need_time=False):
    """Keep only well-formed rows of the quality we asked for.

    Everything here arrives from a third-party API, so a row with a missing or
    non-numeric price is dropped rather than trusted into the arithmetic.
    """
    out = []
    for e in entries or []:
        if not isinstance(e, dict) or bool(e.get("hq")) != hq:
            continue
        price, qty = e.get("pricePerUnit"), e.get("quantity")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        if not isinstance(qty, (int, float)) or qty <= 0:
            continue
        if need_time and not isinstance(e.get("timestamp"), (int, float)):
            continue
        out.append(e)
    return out


# ----------------------------------------------------------------------------
# Step 1 -- work out the map, then sweep every marketable item
# ----------------------------------------------------------------------------

def discover(F):
    """Which data centers and worlds exist on each side of the trade."""
    F.report("Looking up worlds", 0.0)
    dcs = F.get("https://universalis.app/api/v2/data-centers") or []
    worlds_all = {w["id"]: w["name"]
                  for w in (F.get("https://universalis.app/api/v2/worlds") or [])}
    if not dcs or not worlds_all:
        # Nothing at all came back -- find out why so we can say so plainly.
        raise Unreachable(health()[1])

    oce = next((d for d in dcs if d["name"] == OCE_DC), None)
    if not oce:
        raise RuntimeError(f"Universalis doesn't list a data center called {OCE_DC!r}.")
    na_dcs = [d for d in dcs if d.get("region") == NA_REGION]
    if not na_dcs:
        raise RuntimeError(f"Universalis listed no data centers in {NA_REGION!r}.")

    def worlds_of(dc):
        return {wid: worlds_all[wid] for wid in dc["worlds"] if wid in worlds_all}

    return {
        "worlds_all": worlds_all,
        # {side: {dc_name: {world_id: world_name}}}
        "oce": {OCE_DC: worlds_of(oce)},
        "na": {d["name"]: worlds_of(d) for d in na_dcs},
    }


def sweep(F):
    items = F.get("https://universalis.app/api/v2/marketable") or []
    if not items:
        raise Unreachable(health()[1])
    F.report(f"Sweeping {len(items):,} marketable items", 0.0)
    batches = chunks(items, 100)

    def pull(arg):
        scope, batch = arg
        return F.get(f"https://universalis.app/api/v2/aggregated/{scope}/"
                     + ",".join(map(str, batch)))

    agg = {}
    spans = {"oce": (0.02, 0.14), "na": (0.14, 0.26)}
    for scope, key, label in ((OCE_DC, "oce", "Pricing Materia"),
                              (NA_PROBE_DC, "na", "Pricing NA")):
        F.phase(*spans[key])
        table = {}
        for res in F.map(pull, [(scope, b) for b in batches], label):
            for row in (res or {}).get("results", []):
                table[row["itemId"]] = row
        agg[key] = table
    return agg


# ----------------------------------------------------------------------------
# Step 2 -- shortlist candidates from the cheap aggregate data
# ----------------------------------------------------------------------------

def shortlist(F, agg):
    """Rank rough opportunities in both directions before paying for detail."""
    now = time.time()

    def scan(buy_key, sell_key, buy_scope, sell_scope):
        rows = []
        for sid, sell in agg[sell_key].items():
            buy = agg[buy_key].get(sid)
            if not buy:
                continue
            for q in ("nq", "hq"):
                buy_price = dig(buy, q, "minListing", buy_scope, "price")
                board = dig(sell, q, "minListing", sell_scope, "price")
                avg = dig(sell, q, "averageSalePrice", sell_scope, "price")
                vel = dig(sell, q, "dailySaleVelocity", sell_scope, "quantity") or 0.0
                last = dig(sell, q, "recentPurchase", sell_scope, "timestamp")
                if not buy_price or buy_price <= 0 or vel <= 0.3:
                    continue
                refs = [p for p in (board, avg) if p]
                if not refs:
                    continue
                profit = min(refs) * TAX - buy_price
                if profit <= 0 or profit / buy_price < MIN_MARGIN:
                    continue
                if last and (now - last / 1000) / 86400 > MAX_SALE_AGE_DAYS:
                    continue
                rows.append({"id": int(sid), "hq": q == "hq",
                             "score": profit * vel, "profit": profit})
        return rows

    # NA -> Materia: buy at the NA region floor, sell into the Materia DC.
    forward = scan("na", "oce", "region", "dc")
    # Materia -> NA: buy at the Materia DC floor, sell into the NA region.
    reverse = scan("oce", "na", "dc", "region")

    tiers = {
        "big": sorted([r for r in forward if r["profit"] >= BIG_TICKET_FLOOR],
                      key=lambda r: -r["score"])[:CANDIDATES_PER_TIER],
        "bulk": sorted([r for r in forward if r["profit"] < BIG_TICKET_FLOOR],
                       key=lambda r: -r["score"])[:CANDIDATES_PER_TIER],
        "rev": sorted(reverse, key=lambda r: -r["score"])[:CANDIDATES_PER_TIER],
    }
    ids = sorted({r["id"] for tier in tiers.values() for r in tier})
    F.report(f"{len(forward) + len(reverse):,} rough routes -> "
             f"inspecting {len(ids)} items", 0.0)
    return tiers, ids


# ----------------------------------------------------------------------------
# Step 3 -- deep fetch: real listings and sale history, per data center
# ----------------------------------------------------------------------------

def deep_fetch(F, ids, geo):
    names = {}
    for batch in chunks(ids, 100):
        res = F.get("https://v2.xivapi.com/api/sheet/Item?rows="
                    + ",".join(map(str, batch))
                    + "&fields=Name,StackSize,ItemUICategory.Name")
        for row in (res or {}).get("rows", []):
            f = row["fields"]
            cat = f.get("ItemUICategory") or {}
            names[row["row_id"]] = {"n": f["Name"], "s": f.get("StackSize") or 1,
                                    "c": (cat.get("fields") or {}).get("Name") or ""}

    # Query each data center separately. A region-wide history query returns the
    # most recent N sales across every world at once, which on a busy item covers
    # only a few hours and leaves quiet worlds with nothing. Per-DC windows are
    # longer and give each world a fair count.
    scopes = list(geo["oce"]) + list(geo["na"])

    def pull(arg):
        """Fetch a group of items in one request, splitting if it's too much.

        A 504 means the server ran out of time building the response, so the
        fix is to ask for fewer items -- halving repeatedly bottoms out at one
        item per request, which always works.
        """
        scope, group = arg
        payload, status = F.fetch(
            f"https://universalis.app/api/v2/{scope}/{','.join(map(str, group))}"
            f"?listings=100&entries=200")

        if payload is None and status in (502, 503, 504):
            if len(group) > 1:
                half = len(group) // 2
                out = {}
                for part in (group[:half], group[half:]):
                    out.update(pull((scope, part))[1])
                return scope, out
            # One item and still too slow: nothing left to split, so it's lost.
            F.fails += 1

        if not isinstance(payload, dict):
            return scope, {}
        # One id comes back as the item itself; several come back under "items".
        if "items" in payload and isinstance(payload["items"], dict):
            return scope, {int(k): v for k, v in payload["items"].items()
                           if isinstance(v, dict)}
        if len(group) == 1 and payload.get("listings") is not None:
            return scope, {group[0]: payload}
        return scope, {}

    jobs = [(scope, ids[i:i + BATCH])
            for scope in scopes for i in range(0, len(ids), BATCH)]
    scan_log(f"deep fetch: {len(ids)} items x {len(scopes)} scopes "
             f"= {len(jobs)} requests at {BATCH} items each")
    F.phase(0.27, 0.96)
    data = {}
    for scope, found in F.map(pull, jobs, "Reading market boards"):
        for iid, payload in found.items():
            data[f"{scope}|{iid}"] = slim(payload)
    return names, data


def slim(payload):
    """Keep only the fields the analysis reads.

    Seller ids, materia slots and city codes go; prices, quantities and the
    retainer's name stay. The name is what lets the app pick your own listings
    out of a wall -- without it there is no way to tell which row is yours, and
    no way to notice one has gone. It is public information shown on any market
    board in the game, and it never leaves this machine.
    """
    def row(e, with_time):
        out = {k: e.get(k) for k in ("worldID", "pricePerUnit", "quantity", "hq")}
        if with_time:
            out["timestamp"] = e.get("timestamp")
        else:
            out["r"] = e.get("retainerName")
        return out

    return {
        "listings": [row(l, False) for l in payload.get("listings") or []
                     if isinstance(l, dict)],
        "recentHistory": [row(h, True) for h in payload.get("recentHistory") or []
                          if isinstance(h, dict)],
    }


# ----------------------------------------------------------------------------
# Step 4 -- economics
# ----------------------------------------------------------------------------

def buy_side(data, iid, hq, dc_map, worlds_all):
    """Cheapest source across a whole side, and the one world holding the most.

    You can travel freely on the buying side, so price is the regional floor --
    but shopping is much less painful from a single world, which is what the
    "best stop" is for.
    """
    listings = []
    for dc in dc_map:
        payload = data.get(f"{dc}|{iid}")
        if isinstance(payload, dict):
            listings += clean(payload.get("listings"), hq)
    if not listings:
        return None
    listings.sort(key=lambda l: l["pricePerUnit"])

    # Ignore listings priced absurdly below the rest before picking a floor.
    # One person dumping a unit at 33 gil against a going rate of 2,200 (seen in
    # practice) would otherwise become the anchor, inflate the apparent margin
    # past the sanity cap, and delete a good item from the tables entirely.
    # Comparing against the median rather than counting units keeps thin,
    # expensive items -- where every listing really is one unit -- intact.
    mid = st.median([l["pricePerUnit"] for l in listings])
    real = [l for l in listings if l["pricePerUnit"] >= mid * OUTLIER_FLOOR] or listings

    floor = real[0]["pricePerUnit"]
    cheap = [l for l in real if l["pricePerUnit"] <= floor * NEAR_MIN]

    by_world = defaultdict(list)
    for l in cheap:
        by_world[l.get("worldID")].append(l)

    def rank(kv):
        units = sum(x["quantity"] for x in kv[1])
        spend = sum(x["pricePerUnit"] * x["quantity"] for x in kv[1])
        return units, -spend / units

    wid, best = max(by_world.items(), key=rank)
    units = sum(x["quantity"] for x in best)
    spend = sum(x["pricePerUnit"] * x["quantity"] for x in best)

    # Keep each world's own board so the detail panel can show the buy side too,
    # not just the one world we happened to pick as cheapest.
    per_world = defaultdict(list)
    for l in listings:
        per_world[l.get("worldID")].append(l)
    boards = {}
    for w, rows in per_world.items():
        name = worlds_all.get(w)
        if not name:
            continue
        rows.sort(key=lambda l: l["pricePerUnit"])
        boards[name] = {
            "wall": [[l["pricePerUnit"], l["quantity"], l.get("r")]
                     for l in rows[:10]],
            "hist": [],
            "s": rows[0]["pricePerUnit"],
            "l": sum(l["quantity"] for l in rows),
        }

    return {"stop": worlds_all.get(wid, "?"), "stop_units": units,
            "buy": spend / units, "region_units": sum(l["quantity"] for l in cheap),
            "boards": boards}


def sell_side(data, iid, hq, dc_map):
    """Board state and demand rate for every world you might sell on.

    Each data center is measured against its own observation window -- the span
    its 200 recorded sales cover -- so a world in a quiet DC isn't judged against
    a busy DC's clock, and a short burst can't be read as a permanent rate.
    """
    per, windows = {}, {}
    for dc, worlds in dc_map.items():
        payload = data.get(f"{dc}|{iid}")
        if not isinstance(payload, dict):
            continue
        listings = clean(payload.get("listings"), hq)
        history = clean(payload.get("recentHistory"), hq, need_time=True)
        if len(history) < MIN_DC_SALES:
            continue
        stamps = [h["timestamp"] for h in history]
        window = max((max(stamps) - min(stamps)) / 86400.0, 0.5)
        dc_median = st.median([h["pricePerUnit"] for h in history])
        windows[dc] = round(window, 1)

        for wid, wname in worlds.items():
            w_list = sorted([l for l in listings if l.get("worldID") == wid],
                            key=lambda l: l["pricePerUnit"])
            w_hist = [h for h in history if h.get("worldID") == wid]
            units = sum(h["quantity"] for h in w_hist)
            prices = [h["pricePerUnit"] for h in w_hist]
            refs = [p for p in (w_list[0]["pricePerUnit"] if w_list else None,
                                st.median(prices) if prices else None,
                                dc_median * PRICE_SANITY) if p]
            per[wname] = {
                # A bounded slice of the raw board, so the app can show the
                # actual wall and recent sales without re-reading the cache.
                # Third element is the retainer, for spotting your own listings.
                "wall": [[l["pricePerUnit"], l["quantity"], l.get("r")]
                         for l in w_list[:10]],
                "hist": [[h["pricePerUnit"], h["quantity"], h["timestamp"]]
                         for h in sorted(w_hist, key=lambda x: -x["timestamp"])[:14]],
                "sell": round(min(refs)),
                "ahead": sum(l["quantity"] for l in w_list
                             if l["pricePerUnit"] <= min(refs)),
                "listed": sum(l["quantity"] for l in w_list),
                "rate": units / window,
                "sold": units,
                "sales": len(w_hist),
                "win": round(window, 1),
                "dc": dc,
            }
    return per or None


def build_row(iid, hq, meta, data, geo, tier):
    """One item's row for one tier, or None if it isn't tradeable.

    Shared by the full scan and the single-item refresh so both produce exactly
    the same shape -- a refreshed row has to drop straight back into the table.
    """
    spec = TIERS[tier]
    buy_map, sell_map = geo[spec["buy"]], geo[spec["sell"]]
    worlds_all = geo["worlds_all"]
    buy = buy_side(data, iid, hq, buy_map, worlds_all)
    per = sell_side(data, iid, hq, sell_map)
    if not buy or not per:
        return None
    return _assemble(iid, hq, meta, buy, per, tier)


def analyse(tiers, names, data, geo):
    worlds_all = geo["worlds_all"]
    out = {}
    for tier, cands in tiers.items():
        spec = TIERS[tier]
        buy_map, sell_map = geo[spec["buy"]], geo[spec["sell"]]
        rows = []
        for c in cands:
            iid, hq = c["id"], c["hq"]
            meta = names.get(iid)
            if not meta:
                continue
            buy = buy_side(data, iid, hq, buy_map, worlds_all)
            per = sell_side(data, iid, hq, sell_map)
            if not buy or not per:
                continue
            built = _assemble(iid, hq, meta, buy, per, tier)
            if built:
                rows.append(built)
        rows.sort(key=lambda r: -max(v["g"] for v in r["w"].values()))
        out[tier] = rows
    return out


def _assemble(iid, hq, meta, buy, per, tier):
    """Turn one item's buy/sell figures into the row the tables display."""
    if True:
        if True:  # noqa: retained indentation from analyse(); dedented below
            stack = meta["s"]
            boards = {
                wname: {"wall": v["wall"], "hist": v["hist"], "s": v["sell"],
                        "a": v["ahead"], "l": v["listed"], "r": round(v["rate"], 2),
                        "so": v["sold"], "ns": v["sales"], "win": v["win"]}
                for wname, v in per.items()
            }
            worlds = {}
            for wname, v in per.items():
                profit = v["sell"] * TAX - buy["buy"]
                if profit <= 0:
                    continue
                margin = profit / buy["buy"]
                if margin < MIN_MARGIN or margin > MAX_MARGIN:
                    continue
                # One retainer slot holds one listing of up to a full stack, and
                # earns whatever that world absorbs per day, whichever is less.
                # This is a *rate*, and it assumes you can keep restocking.
                throughput = min(v["rate"], stack)
                # What one shopping run is actually worth: you can only buy what
                # is listed cheaply, and there's no point buying more than the
                # board will absorb in a week. On a shallow market -- which is
                # most of Materia -- this is far below the daily rate above.
                units = min(buy["region_units"], max(v["rate"] * 7, 1))
                worlds[wname] = {
                    "wall": v["wall"], "hist": v["hist"],
                    "s": v["sell"], "p": round(profit), "m": round(margin * 100),
                    "a": v["ahead"], "l": v["listed"], "r": round(v["rate"], 2),
                    "so": v["sold"], "ns": v["sales"], "win": v["win"],
                    "g": round(profit * throughput),
                    "t": round(profit * units),
                    "u": round(units),
                    "d": round(1 / v["rate"], 1) if v["rate"] > 0 else None,
                }
            if not worlds:
                return None
            return {"id": iid, "n": meta["n"], "c": meta["c"],
                    "q": "HQ" if hq else "NQ", "st": stack,
                    "b": round(buy["buy"]), "stop": buy["stop"],
                    "su": buy["stop_units"], "nau": buy["region_units"],
                    "w": worlds, "boards": boards,
                    "buyboards": buy.get("boards") or {}}



# ----------------------------------------------------------------------------
# Looking up one item, without scanning everything
# ----------------------------------------------------------------------------
# A full scan prices 590 items and takes minutes. Most of the time you want one
# item, which is five requests and a few seconds. This keeps a local name index
# so the search box works instantly and offline.

def build_name_index(F, report=None):
    """id -> name for every marketable item. Fetched once, then cached."""
    report = report or (lambda m, f=None: None)
    marketable = set(F.get("https://universalis.app/api/v2/marketable") or [])
    if not marketable:
        raise Unreachable(health()[1])

    index, after, page = {}, 0, 0
    while True:
        F.check()
        page += 1
        report(f"Building item index, {len(index):,} named", min(page / 90, 0.95))
        rows = (F.get("https://v2.xivapi.com/api/sheet/Item"
                      f"?limit=500&after={after}&fields=Name") or {}).get("rows")
        if not rows:
            break
        for row in rows:
            rid = row.get("row_id")
            name = (row.get("fields") or {}).get("Name")
            if rid in marketable and isinstance(name, str) and name.strip():
                index[rid] = name
        after = rows[-1]["row_id"]
        if len(rows) < 500:
            break

    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = NAMES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"built": datetime.now().isoformat(timespec="seconds"),
                       "items": {str(k): v for k, v in index.items()}}, fh)
        os.replace(tmp, NAMES)
    except OSError:
        pass
    return index


def load_name_index():
    """The cached index, or None if it hasn't been built yet."""
    try:
        with open(NAMES, encoding="utf-8-sig") as fh:
            blob = json.load(fh)
        items = blob.get("items") if isinstance(blob, dict) else None
        if not isinstance(items, dict) or not items:
            return None
        return {int(k): v for k, v in items.items()}
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return None


def search_names(index, query, limit=60):
    """Substring match, with names that start with the query ranked first."""
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    starts, contains = [], []
    for iid, name in index.items():
        low = name.lower()
        if low.startswith(q):
            starts.append((iid, name))
        elif q in low:
            contains.append((iid, name))
    starts.sort(key=lambda kv: kv[1])
    contains.sort(key=lambda kv: kv[1])
    return (starts + contains)[:limit]


def read_board(item_id, quality, world, report=None, should_stop=None):
    """One board, fresh from the API. Returns (listings, history)."""
    F = Fetcher(report or (lambda m, f=None: None), should_stop or (lambda: False))
    payload = F.get(f"https://universalis.app/api/v2/{world}/{item_id}"
                    f"?listings=100&entries=50")
    if not isinstance(payload, dict):
        raise Unreachable("Couldn't read that market board.")
    hq = quality == "HQ"
    listings = sorted(clean(payload.get("listings"), hq),
                      key=lambda l: l["pricePerUnit"])
    history = clean(payload.get("recentHistory"), hq, need_time=True)
    return listings, history


def standing(rows, listing, mine_names, price_of, qty_of, name_of):
    """Where one of your listings stands against *other* sellers on a board.

    Your own listings are excluded from every count. Ten stacks of your own at
    the same price are not ten people undercutting you, and being the tenth row
    on the board is not the same as being tenth in line -- treating them as
    competition made the app report you undercut by yourself.
    """
    mine_price = price_of(listing)
    others = [x for x in rows if str(name_of(x) or "").lower() not in mine_names]
    cheaper = [x for x in others if price_of(x) < mine_price]
    return {
        "under": sum(qty_of(x) or 0 for x in cheaper),
        "sellers_under": len(cheaper),
        "position": len(cheaper) + 1,
        "of": len(others) + 1,
        "ties": sum(1 for x in others if price_of(x) == mine_price),
    }


def mine(listings, names_on_world):
    """The listings belonging to you, given your retainer names on that world.

    Retainer names are unique only within a world -- two players on different
    servers can both own a "Kupo" -- so the caller resolves which of your names
    apply to the board being examined and passes just those.
    """
    wanted = {str(n).strip().lower() for n in (names_on_world or []) if str(n).strip()}
    if not wanted:
        return []
    return [l for l in listings
            if str(l.get("retainerName") or l.get("r") or "").lower() in wanted]


def recheck_boards(boards, retainers, report=None, should_stop=None):
    """Re-read boards live and find where your retainers sit on each now.

    `boards` is an iterable of (item_id, quality, world). Returns the same shape
    find_my_listings produces, so a refreshed view drops straight into the table
    -- and a listing that has gone simply isn't in the result, which is how the
    app can finally say something sold.
    """
    report = report or (lambda m, f=None: None)
    F = Fetcher(report, should_stop or (lambda: False))
    boards = list(boards)
    if not boards:
        return [], set()

    def pull(board):
        item_id, _quality, world = board
        return board, F.get(f"https://universalis.app/api/v2/{world}/{item_id}"
                            f"?listings=100&entries=20")

    found, checked = [], set()
    F.phase(0.0, 0.95)
    for board, payload in F.map(pull, boards, "Re-reading your boards"):
        item_id, quality, world = board
        if not isinstance(payload, dict):
            continue
        checked.add(board)
        hq = quality == "HQ"
        rows = sorted(clean(payload.get("listings"), hq),
                      key=lambda l: l["pricePerUnit"])
        wanted = retainers_on_world(retainers, world)
        mine_count = sum(
            1 for x in rows
            if str(x.get("retainerName") or x.get("r") or "").strip().lower()
            in wanted)
        for l in rows:
            who = str(l.get("retainerName") or l.get("r") or "").strip()
            if who.lower() not in wanted:
                continue
            rank = standing(rows, l, wanted,
                            lambda x: x["pricePerUnit"],
                            lambda x: x.get("quantity"),
                            lambda x: x.get("retainerName") or x.get("r"))
            found.append({
                "id": item_id, "name": None, "q": quality, "world": world,
                "price": l["pricePerUnit"], "qty": l["quantity"],
                "retainer": who, "mine_on_board": mine_count, **rank,
            })
    return found, checked


def sweep_for_listings(retainers, names=None, report=None, should_stop=None):
    """Find every listing you have, by sweeping whole worlds.

    The cached scan only covers the few hundred items the arbitrage tables care
    about -- about 3.5% of the catalogue -- so looking for your listings in it
    finds almost nothing. This reads every marketable item on each world you
    have a retainer registered on instead, which is the only way to actually
    find all of them. One world takes well under a minute.
    """
    report = report or (lambda m, f=None: None)
    F = Fetcher(report, should_stop or (lambda: False))
    names = names or {}

    worlds = sorted({r["world"] for r in retainers
                     if isinstance(r, dict) and r.get("world")})
    if not worlds:
        return [], []

    items = F.get("https://universalis.app/api/v2/marketable") or []
    if not items:
        raise Unreachable(health()[1])
    groups = chunks(items, FIND_BATCH)

    found = []
    for index, world in enumerate(worlds):
        wanted = retainers_on_world(retainers, world)
        if not wanted:
            continue
        span = 1.0 / len(worlds)
        F.phase(index * span, (index + 1) * span * 0.98)

        def pull(group, world=world):
            ids = ",".join(map(str, group))
            payload, status = F.fetch(
                f"https://universalis.app/api/v2/{world}/{ids}"
                f"?listings=100&entries=0")
            if payload is None and status in (502, 503, 504) and len(group) > 1:
                half = len(group) // 2
                out = {}
                for part in (group[:half], group[half:]):
                    out.update(pull(part))
                return out
            if not isinstance(payload, dict):
                return {}
            if isinstance(payload.get("items"), dict):
                return {int(k): v for k, v in payload["items"].items()
                        if isinstance(v, dict)}
            if len(group) == 1 and payload.get("listings") is not None:
                return {group[0]: payload}
            return {}

        for batch in F.map(pull, groups, f"Searching {world}"):
            for item_id, payload in batch.items():
                rows = sorted([l for l in (payload.get("listings") or [])
                               if isinstance(l, dict)
                               and isinstance(l.get("pricePerUnit"), (int, float))],
                              key=lambda l: l["pricePerUnit"])
                mine_count = sum(
                    1 for x in rows
                    if str(x.get("retainerName") or "").strip().lower() in wanted)
                for l in rows:
                    who = str(l.get("retainerName") or "").strip()
                    if who.lower() not in wanted:
                        continue
                    rank = standing(rows, l, wanted,
                                    lambda x: x["pricePerUnit"],
                                    lambda x: x.get("quantity"),
                                    lambda x: x.get("retainerName"))
                    found.append({
                        "id": item_id,
                        "name": names.get(item_id) or f"item {item_id}",
                        "q": "HQ" if l.get("hq") else "NQ",
                        "world": world,
                        "price": l["pricePerUnit"],
                        "qty": l.get("quantity") or 0,
                        "retainer": who,
                        "mine_on_board": mine_count,
                        **rank,
                    })

    # Name only what was actually found -- a couple of requests, rather than
    # building the whole 17,000-item index just to label a few dozen rows.
    unknown = sorted({r["id"] for r in found if not names.get(r["id"])})
    if unknown:
        resolved = {}
        for group in chunks(unknown, 100):
            res = F.get("https://v2.xivapi.com/api/sheet/Item?rows="
                        + ",".join(map(str, group)) + "&fields=Name")
            for row in (res or {}).get("rows", []):
                label = (row.get("fields") or {}).get("Name")
                if isinstance(label, str) and label.strip():
                    resolved[row["row_id"]] = label
        for r in found:
            if r["id"] in resolved:
                r["name"] = resolved[r["id"]]

    found.sort(key=lambda r: (r["name"], r["world"], r["price"]))
    return found, worlds


def find_my_listings(retainers, world_names_for):
    """Every listing in the last scan that belongs to one of your retainers.

    The scan already stores the retainer name on each listing, so this is a walk
    over the cached snapshot -- no requests, instant. It only sees items the last
    scan covered (a few hundred of the ~17,000 marketable ones), so it finds what
    you have listed among the things the app tracks, not literally everything.

    `world_names_for` maps a world id to its name, so a retainer registered on
    one world can't match a namesake on another.
    """
    try:
        with open(SNAPSHOT, encoding="utf-8-sig") as fh:
            snap = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(snap, dict):
        return []
    names = snap.get("names") or {}
    data = snap.get("data") or {}

    # Which of your retainer names apply on each world, resolved once.
    by_world = {}
    found, seen = [], set()

    for key, payload in data.items():
        if not isinstance(payload, dict):
            continue
        try:
            _scope, raw_id = key.split("|", 1)
            item_id = int(raw_id)
        except (ValueError, AttributeError):
            continue
        listings = payload.get("listings") or []
        if not listings:
            continue
        # Rank within each world, so we can say where you sit on that board.
        ranked = {}
        for l in sorted([x for x in listings if isinstance(x, dict)],
                        key=lambda x: (x.get("worldID"), x.get("pricePerUnit") or 0)):
            ranked.setdefault(l.get("worldID"), []).append(l)

        for wid, rows in ranked.items():
            world = world_names_for.get(wid)
            if not world:
                continue
            if world not in by_world:
                by_world[world] = retainers_on_world(retainers, world)
            wanted = by_world[world]
            if not wanted:
                continue
            mine_count = sum(1 for x in rows
                             if str(x.get("r") or "").strip().lower() in wanted)
            for position, l in enumerate(rows, 1):
                who = str(l.get("r") or "").strip()
                if who.lower() not in wanted:
                    continue
                rank = standing(rows, l, wanted,
                                lambda x: x.get("pricePerUnit") or 0,
                                lambda x: x.get("quantity"),
                                lambda x: x.get("r"))
                # The same board appears under both a DC and a region scope, so
                # the same listing can be seen twice. Keep one of each.
                sig = (item_id, wid, l.get("pricePerUnit"), l.get("quantity"),
                       who.lower(), position)
                if sig in seen:
                    continue
                seen.add(sig)
                meta = names.get(str(item_id)) or {}
                found.append({
                    "id": item_id,
                    "name": meta.get("n") or f"item {item_id}",
                    "q": "HQ" if l.get("hq") else "NQ",
                    "world": world,
                    "price": l.get("pricePerUnit"),
                    "qty": l.get("quantity"),
                    "retainer": who,
                    "mine_on_board": mine_count,
                    **rank,
                })
    found.sort(key=lambda r: (r["name"], r["world"], r["price"] or 0))
    return found


def retainers_on_world(retainers, world):
    """Your retainer names on one world, lowercased. Blank world matches anywhere."""
    target = (world or "").strip().lower()
    out = set()
    for entry in retainers or []:
        if isinstance(entry, str):
            name, rworld = entry.strip(), ""
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            rworld = str(entry.get("world") or "").strip()
        else:
            continue
        if name and (not rworld or rworld.lower() == target):
            out.add(name.lower())
    return out


def refresh_one(item_id, hq, meta, tier, geo, report=None, should_stop=None):
    """Re-price a single item for one tier. Five requests, a few seconds.

    Returns the same row shape a scan produces, so it can be dropped straight
    back into the table in place of the stale one. None means the item no
    longer clears the margin anywhere -- it isn't tradeable right now.
    """
    report = report or (lambda m, f=None: None)
    F = Fetcher(report, should_stop or (lambda: False))
    scopes = list(geo["oce"]) + list(geo["na"])

    def pull(scope):
        return scope, F.get(f"https://universalis.app/api/v2/{scope}/{item_id}"
                            f"?listings=100&entries=200")

    data = {}
    F.phase(0.0, 0.9)
    for scope, payload in F.map(pull, scopes, "Re-reading market boards"):
        if isinstance(payload, dict) and payload.get("listings") is not None:
            data[f"{scope}|{item_id}"] = slim(payload)
    if not data:
        raise Unreachable("Couldn't read any market board for that item.")
    return build_row(item_id, hq, meta, data, geo, tier)


def lookup_item(item_id, geo, report=None, should_stop=None):
    """Price one item on every world, both regions, both qualities.

    Returns one row per world per quality rather than a list of routes. A route
    is a conclusion; what you actually want to see first is the raw picture --
    what it costs on each board and what it goes for there -- and then let the
    best route fall out of that.
    """
    report = report or (lambda m, f=None: None)
    F = Fetcher(report, should_stop or (lambda: False))
    scopes = list(geo["oce"]) + list(geo["na"])
    region_of = {}
    for dc in geo["oce"]:
        region_of[dc] = "Materia"
    for dc in geo["na"]:
        region_of[dc] = "North America"

    def pull(scope):
        return scope, F.get(f"https://universalis.app/api/v2/{scope}/{item_id}"
                            f"?listings=100&entries=200")

    data = {}
    F.phase(0.0, 0.9)
    for scope, payload in F.map(pull, scopes, "Reading market boards"):
        if isinstance(payload, dict) and payload.get("listings") is not None:
            data[f"{scope}|{item_id}"] = slim(payload)
    if not data:
        raise Unreachable("Couldn't read any market board for that item.")

    out = {}
    for quality, hq in (("HQ", True), ("NQ", False)):
        rows = []
        for scope in scopes:
            payload = data.get(f"{scope}|{item_id}")
            if not isinstance(payload, dict):
                continue
            worlds = (geo["oce"].get(scope) or geo["na"].get(scope) or {})
            listings = clean(payload.get("listings"), hq)
            history = clean(payload.get("recentHistory"), hq, need_time=True)
            stamps = [h["timestamp"] for h in history]
            window = max((max(stamps) - min(stamps)) / 86400.0, 0.5) if stamps else 0
            dc_median = st.median([h["pricePerUnit"] for h in history]) if history else None

            for wid, wname in worlds.items():
                mine = sorted([l for l in listings if l.get("worldID") == wid],
                              key=lambda l: l["pricePerUnit"])
                sales = [h for h in history if h.get("worldID") == wid]
                units = sum(l["quantity"] for l in mine)
                sold = sum(h["quantity"] for h in sales)
                prices = [h["pricePerUnit"] for h in sales]
                # What you could realistically get here: undercut the board, but
                # not above what it has actually been selling for.
                refs = [p for p in (mine[0]["pricePerUnit"] if mine else None,
                                    st.median(prices) if prices else None,
                                    dc_median * PRICE_SANITY if dc_median else None)
                        if p]
                rows.append({
                    "id": item_id,
                    "world": wname,
                    "dc": scope,
                    "region": region_of.get(scope, "?"),
                    "buy": mine[0]["pricePerUnit"] if mine else None,
                    "units": units,
                    "listings": len(mine),
                    "sell": round(min(refs)) if refs else None,
                    "sold": sold,
                    "rate": round(sold / window, 2) if window else 0.0,
                    "wall": [[l["pricePerUnit"], l["quantity"], l.get("r")]
                             for l in mine[:10]],
                    "hist": [[h["pricePerUnit"], h["quantity"], h["timestamp"]]
                             for h in sorted(sales, key=lambda x: -x["timestamp"])[:14]],
                })

        if not rows:
            continue
        priced = [r for r in rows if r["buy"] is not None]
        cheapest = min(priced, key=lambda r: r["buy"]) if priced else None
        sellable = [r for r in rows if r["sell"] is not None]
        dearest = max(sellable, key=lambda r: r["sell"]) if sellable else None

        # Net per unit if you bought at the cheapest board anywhere and sold here.
        for r in rows:
            if cheapest and r["sell"] is not None:
                r["net"] = round(r["sell"] * TAX - cheapest["buy"])
                r["margin"] = (round(r["net"] / cheapest["buy"] * 100)
                               if cheapest["buy"] else 0)
            else:
                r["net"] = r["margin"] = None

        rows.sort(key=lambda r: (r["region"], -(r["sell"] or 0)))
        out[quality] = {
            "rows": rows,
            "cheapest": cheapest,
            "dearest": dearest,
            "best": max((r for r in rows if r["net"] is not None),
                        key=lambda r: r["net"], default=None),
        }
    return out


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Scan history -- so the app can show which margins are opening and closing
# ----------------------------------------------------------------------------
# Full snapshots are far too big to keep several of, so history stores only what
# a trend needs: the net profit and sell price for each item on each world.

def _digest(tables):
    out = {}
    for tier, rows in tables.items():
        for r in rows:
            for wname, v in r["w"].items():
                out[f"{r['id']}|{r['q']}|{wname}"] = [v["p"], v["s"]]
    return out


def read_history():
    try:
        with open(HISTORY, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def append_history(tables, stamp):
    log = read_history()
    log.append({"stamp": stamp, "rows": _digest(tables)})
    del log[:-HISTORY_KEEP]
    os.makedirs(CACHE, exist_ok=True)
    tmp = HISTORY + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(log, fh)
        os.replace(tmp, HISTORY)
    except OSError:
        pass
    return log


def attach_trends(tables, previous):
    """Mark each row with how its profit moved since the scan before this one.

    `dp` is the change in net profit per unit, `new` means it wasn't tradeable
    on that world last time. Rows get None when there's nothing to compare to,
    which the app renders as a dash rather than a misleading zero.
    """
    rows = (previous or {}).get("rows") or {}
    for tier_rows in tables.values():
        for r in tier_rows:
            for wname, v in r["w"].items():
                before = rows.get(f"{r['id']}|{r['q']}|{wname}")
                if not rows:
                    v["dp"], v["new"] = None, False
                elif before is None:
                    v["dp"], v["new"] = None, True
                else:
                    v["dp"], v["new"] = v["p"] - before[0], False
    return tables


def snapshot_age_hours():
    """How old the cached scan is, or None if there isn't one."""
    try:
        return (time.time() - os.path.getmtime(SNAPSHOT)) / 3600.0
    except OSError:
        return None


def check_positions(positions, retainers=None, report=None, should_stop=None):
    """Look up just the items you've listed, on just the worlds you listed them.

    One request per board -- not per listing -- so several stacks of the same
    item cost a single lookup, and the whole check takes seconds rather than
    the minutes a full scan needs. It reports where your price sits
    on the board now; it cannot tell you an item sold, because listings carry no
    identity we could match yours against.
    """
    report = report or (lambda m, f=None: None)
    should_stop = should_stop or (lambda: False)
    F = Fetcher(report, should_stop)
    if not positions:
        return {}

    def usable(pos):
        # positions.json is plain JSON on disk; one bad line shouldn't abort
        # the check for every other listing you're tracking.
        return (isinstance(pos, dict)
                and isinstance(pos.get("id"), int)
                and isinstance(pos.get("price"), (int, float))
                and isinstance(pos.get("world"), str)
                and pos["world"].isalnum())

    # Several of your listings can sit on the same board -- the game puts each
    # stack up separately -- so read each board once and judge every listing you
    # have on it against that one snapshot.
    boards = {}
    for key, pos in positions.items():
        if usable(pos):
            boards.setdefault((pos["id"], pos["q"], pos["world"]), []).append(key)

    def pull(board):
        item_id, _quality, world = board
        return board, F.get(f"https://universalis.app/api/v2/{world}/{item_id}"
                            f"?listings=40&entries=20")

    out = {key: {"state": "unknown", "note": "this saved line is unreadable"}
           for key, pos in positions.items() if not usable(pos)}

    F.phase(0.0, 0.95)
    for board, payload in F.map(pull, list(boards), "Checking your listings"):
        keys = boards[board]
        if not isinstance(payload, dict):
            for key in keys:
                out[key] = {"state": "unknown",
                            "note": "couldn't reach Universalis"}
            continue
        hq = board[1] == "HQ"
        listings = sorted(clean(payload.get("listings"), hq),
                          key=lambda l: l["pricePerUnit"])
        history = clean(payload.get("recentHistory"), hq, need_time=True)
        ours = retainers_on_world(retainers, board[2]) if retainers else set()
        others = [l for l in listings
                  if str(l.get("retainerName") or "").lower() not in ours]
        low = others[0]["pricePerUnit"] if others else None

        for key in keys:
            pos = positions[key]
            yours = pos["price"]
            if not listings:
                out[key] = {"state": "empty", "low": None, "under": 0,
                            "note": "nothing listed on that world at all"}
                continue
            # Exclude anything of yours: a cheaper stack of your own is not
            # somebody undercutting you.
            under = sum(l["quantity"] for l in listings
                        if l["pricePerUnit"] < yours
                        and str(l.get("retainerName") or "").lower() not in ours)
            recent = [h for h in history
                      if h["timestamp"] >= pos.get("at", 0) / 1000]
            out[key] = {
                "state": "undercut" if under else "lowest",
                "low": low,
                "under": under,
                "gap": yours - low,
                "sold_since": sum(h["quantity"] for h in recent),
                "note": (f"{under} unit(s) listed below you"
                         if under else "you are still the cheapest"),
            }
    return out


def world_menu(geo, side):
    """Worlds you can sell on for a side, grouped by data center for the picker."""
    return {dc: sorted(worlds.values()) for dc, worlds in geo[side].items()}


def _package(tables, geo, stamp, fails, degraded=False):
    return {
        "tables": tables,
        "stamp": stamp,
        "fails": fails,
        "degraded": degraded,
        # Which world list each tab's picker should offer.
        "menus": {tier: world_menu(geo, spec["sell"]) for tier, spec in TIERS.items()},
    }


def load_geo(F=None):
    """World layout, from the cached scan if there is one, else one request."""
    try:
        with open(SNAPSHOT, encoding="utf-8-sig") as fh:
            snap = json.load(fh)
        g = snap["geo"]
        return {"worlds_all": {int(k): v for k, v in g["worlds_all"].items()},
                "oce": {dc: {int(k): v for k, v in w.items()}
                        for dc, w in g["oce"].items()},
                "na": {dc: {int(k): v for k, v in w.items()}
                       for dc, w in g["na"].items()}}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return discover(F or Fetcher(lambda m, f=None: None, lambda: False))


def load_cached():
    """Return the last scan's results, or None if there isn't a usable one."""
    if not os.path.exists(SNAPSHOT):
        return None
    try:
        with open(SNAPSHOT, encoding="utf-8-sig") as fh:
            snap = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(snap, dict):
        return None       # valid JSON, wrong shape -- treat it as no cache
    if snap.get("version") != SNAPSHOT_VERSION:
        return None       # written by an older layout; a refresh will replace it
    try:
        geo = {"worlds_all": {int(k): v for k, v in snap["geo"]["worlds_all"].items()},
               "oce": {dc: {int(k): v for k, v in w.items()}
                       for dc, w in snap["geo"]["oce"].items()},
               "na": {dc: {int(k): v for k, v in w.items()}
                      for dc, w in snap["geo"]["na"].items()}}
        tables = analyse(snap["tiers"], {int(k): v for k, v in snap["names"].items()},
                         snap["data"], geo)
    except (KeyError, TypeError, ValueError):
        return None
    log = read_history()
    attach_trends(tables, log[-2] if len(log) >= 2 else None)
    return _package(tables, geo, snap["stamp"], 0)


def scan_log(message):
    """Append a timestamped line to scan.log.

    A windowed build has no console, so when a scan misbehaves there is nothing
    to look at. This leaves a trail that says which phase it reached and when.
    """
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(os.path.join(CACHE, "scan.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), message))
    except OSError:
        pass


def run_scan(report=None, should_stop=None):
    """Full refresh. `report(message, fraction)` is called as work proceeds."""
    caller = report or (lambda m, f=None: print(m))

    def report(message, frac=None):
        scan_log(message)
        caller(message, frac)

    should_stop = should_stop or (lambda: False)
    scan_log("=== scan started ===")
    F = Fetcher(report, should_stop)

    geo = discover(F)
    agg = sweep(F)
    tiers, ids = shortlist(F, agg)
    names, data = deep_fetch(F, ids, geo)

    if F.cancelled and not data:
        raise Cancelled()          # stopped before anything useful arrived

    report("Working out per-world economics", 0.97)
    tables = analyse(tiers, names, data, geo)
    scan_log(f"analysis done: " + ", ".join(f"{k}={len(v)}" for k, v in tables.items()))
    log = read_history()
    attach_trends(tables, log[-1] if log else None)
    stamp = datetime.now().strftime("%d %b %Y, %H:%M")

    if F.cancelled:
        # Show what we got, but don't let a half-finished scan overwrite the
        # complete one already on disk, or pollute the trend history.
        covered = len({k.split("|", 1)[1] for k in data})
        scan_log(f"stopped early: {covered}/{len(ids)} items covered, "
                 f"not written to disk")
        pkg = _package(tables, geo, stamp + " (stopped early)", F.fails, True)
        pkg["partial"] = {"covered": covered, "of": len(ids)}
        return pkg

    os.makedirs(CACHE, exist_ok=True)
    scan_log("writing snapshot")
    tmp = SNAPSHOT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": SNAPSHOT_VERSION, "stamp": stamp,
                   "names": {str(k): v for k, v in names.items()},
                   "geo": {"worlds_all": {str(k): v for k, v in geo["worlds_all"].items()},
                           "oce": {dc: {str(k): v for k, v in w.items()}
                                   for dc, w in geo["oce"].items()},
                           "na": {dc: {str(k): v for k, v in w.items()}
                                  for dc, w in geo["na"].items()}},
                   "tiers": tiers, "data": data}, fh)
    os.replace(tmp, SNAPSHOT)
    append_history(tables, stamp)
    scan_log(f"=== scan finished: {F.calls} requests, {F.fails} lost, "
             f"{F.splits} split and retried ===")

    return _package(tables, geo, stamp, F.fails, F.degraded)


if __name__ == "__main__":
    # A Windows console is often cp1252, which can't encode the arrow in the
    # tier labels. Don't let a progress line be what kills a finished scan.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    def cli(msg, frac=None):
        bar = "" if frac is None else f" [{int(frac * 100):3d}%]"
        print(f"\r{msg}{bar}".ljust(72), end="", flush=True)

    try:
        result = run_scan(cli)
    except Cancelled:
        sys.exit("\nStopped.")
    print("\nDone at " + result["stamp"] + ": "
          + ", ".join(f"{len(result['tables'][t])} {TIERS[t]['label'].lower()}"
                      for t in TIERS)
          + (f", {result['fails']} requests failed" if result["fails"] else ""))
