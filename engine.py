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
NEAR_MIN = 1.25               # "cheap" stock = within 25% of the lowest listing

WORKERS = 8                   # concurrent requests; Universalis tolerates this
CANDIDATES_PER_TIER = 200     # items deep-fetched per tier

SNAPSHOT_VERSION = 2          # bumped when the cache layout changes

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
SNAPSHOT = os.path.join(CACHE, "snapshot.json")
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

    @property
    def degraded(self):
        """True when enough requests failed that the results have holes in them."""
        return self.calls > 20 and self.fails / self.calls > 0.1

    def check(self):
        if self.should_stop():
            raise Cancelled()

    def get(self, url, tries=4):
        # Only ever talk to the two APIs this tool is built against, over TLS.
        # A redirect that tries to move us off HTTPS or onto another host is
        # treated as a failure rather than followed.
        if not any(url.startswith(p) for p in ALLOWED_PREFIXES):
            raise ValueError(f"Refusing to fetch an unexpected URL: {url[:80]}")
        self.calls += 1
        for attempt in range(tries):
            self.check()
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=60) as r:
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
                    return payload
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    json.JSONDecodeError, UnicodeDecodeError):
                if attempt == tries - 1:
                    self.fails += 1
                    return None
                time.sleep(1.2 * (attempt + 1))

    def map(self, fn, items, label):
        out, done, total = [], 0, len(items)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            try:
                for res in ex.map(fn, items):
                    done += 1
                    if done % 20 == 0 or done == total:
                        self.report(f"{label} {done}/{total}", done / total)
                    out.append(res)
            except Cancelled:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
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
    for scope, key, label in ((OCE_DC, "oce", "Pricing Materia"),
                              (NA_PROBE_DC, "na", "Pricing NA")):
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
        scope, iid = arg
        return scope, iid, F.get(
            f"https://universalis.app/api/v2/{scope}/{iid}?listings=100&entries=200")

    jobs = [(s, i) for i in ids for s in scopes]
    data = {}
    for scope, iid, payload in F.map(pull, jobs, "Reading market boards"):
        if isinstance(payload, dict):
            data[f"{scope}|{iid}"] = slim(payload)
    return names, data


def slim(payload):
    """Keep only the fields the analysis reads.

    The raw responses carry retainer names, seller IDs, materia slots and city
    codes we never look at. Dropping them shrinks the on-disk cache by roughly
    an order of magnitude and means the file holds nothing but prices.
    """
    def row(e, with_time):
        out = {k: e.get(k) for k in ("worldID", "pricePerUnit", "quantity", "hq")}
        if with_time:
            out["timestamp"] = e.get("timestamp")
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
    floor = listings[0]["pricePerUnit"]
    cheap = [l for l in listings if l["pricePerUnit"] <= floor * NEAR_MIN]

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
    return {"stop": worlds_all.get(wid, "?"), "stop_units": units,
            "buy": spend / units, "region_units": sum(l["quantity"] for l in cheap)}


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
            stack = meta["s"]
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
                    "s": v["sell"], "p": round(profit), "m": round(margin * 100),
                    "a": v["ahead"], "l": v["listed"], "r": round(v["rate"], 2),
                    "so": v["sold"], "ns": v["sales"], "win": v["win"],
                    "g": round(profit * throughput),
                    "t": round(profit * units),
                    "u": round(units),
                    "d": round(1 / v["rate"], 1) if v["rate"] > 0 else None,
                }
            if not worlds:
                continue
            rows.append({"id": iid, "n": meta["n"], "c": meta["c"],
                         "q": "HQ" if hq else "NQ", "st": stack,
                         "b": round(buy["buy"]), "stop": buy["stop"],
                         "su": buy["stop_units"], "nau": buy["region_units"],
                         "w": worlds})
        rows.sort(key=lambda r: -max(v["g"] for v in r["w"].values()))
        out[tier] = rows
    return out


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------

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


def load_cached():
    """Return the last scan's results, or None if there isn't a usable one."""
    if not os.path.exists(SNAPSHOT):
        return None
    try:
        with open(SNAPSHOT, encoding="utf-8") as fh:
            snap = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
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
    return _package(tables, geo, snap["stamp"], 0)


def run_scan(report=None, should_stop=None):
    """Full refresh. `report(message, fraction)` is called as work proceeds."""
    report = report or (lambda m, f=None: print(m))
    should_stop = should_stop or (lambda: False)
    F = Fetcher(report, should_stop)

    geo = discover(F)
    agg = sweep(F)
    tiers, ids = shortlist(F, agg)
    names, data = deep_fetch(F, ids, geo)
    report("Working out per-world economics", 0.97)
    tables = analyse(tiers, names, data, geo)

    stamp = datetime.now().strftime("%d %b %Y, %H:%M")
    os.makedirs(CACHE, exist_ok=True)
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

    return _package(tables, geo, stamp, F.fails, F.degraded)


if __name__ == "__main__":
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
