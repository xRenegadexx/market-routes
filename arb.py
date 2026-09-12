#!/usr/bin/env python3
"""
FFXIV NA -> Materia market board arbitrage scanner.

Buys are sourced anywhere in the North-American region (your NA character can
travel between NA data centers freely). Sales are priced per Materia world,
because market boards are per-world: the only listings you compete with are the
ones on your seller character's home world.

Run:  python arb.py            full refresh, rewrites index.html
      python arb.py --offline  re-render from the last cached fetch (fast)
      python arb.py --top 120  keep more rows per tier

No third-party packages. Python 3.9+.
"""

import argparse
import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ----------------------------------------------------------------------------
# Config -- edit these if you want to change what the scan looks for.
# ----------------------------------------------------------------------------

SELL_DC = "Materia"          # data center you sell on
BUY_SCOPE = "North-America"  # region you buy from
BUY_PROBE_DC = "Aether"      # any NA DC; its "region" aggregates cover all of NA

TAX = 0.95                   # seller receives 95% after the 5% market board tax
BIG_TICKET_FLOOR = 20_000    # gil/unit dividing the two tabs

MIN_MARGIN = 0.12            # 12% -- below this the trip isn't worth the slot
MAX_MARGIN = 15.0            # 1500% -- above this it's a private trade, not demand
MIN_DC_SALES = 4             # sales in the history window before we'll judge demand
MAX_SALE_AGE_DAYS = 7        # the item must have sold on Materia recently
PRICE_SANITY = 1.5           # cap sell price at 1.5x the DC median sale
NEAR_MIN = 1.25              # "cheap" NA stock = within 25% of the lowest listing

WORKERS = 8                  # concurrent requests; Universalis tolerates this fine
CANDIDATES_PER_TIER = 220    # items deep-fetched per tier
ROWS_PER_TIER = 90           # rows written into the page

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
OUT = os.path.join(HERE, "index.html")
TEMPLATE = os.path.join(HERE, "template.html")
UA = {"User-Agent": "ffxiv-arb/1.0 (personal market board tool)"}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Fetcher:
    def __init__(self):
        self.fails = 0

    def get(self, url, tries=4):
        for attempt in range(tries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode())
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    json.JSONDecodeError) as e:
                if attempt == tries - 1:
                    self.fails += 1
                    print(f"    ! gave up on {url[:70]}... ({e})", file=sys.stderr)
                    return None
                time.sleep(1.2 * (attempt + 1))

    def map(self, fn, items, label):
        out, done, total = [], 0, len(items)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for res in ex.map(fn, items):
                done += 1
                if done % 40 == 0 or done == total:
                    pct = done * 100 // total
                    print(f"\r  {label}: {done}/{total} ({pct}%)", end="", flush=True)
                out.append(res)
        print(f"  ({time.time() - t0:.0f}s)")
        return out


F = Fetcher()


def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


# ----------------------------------------------------------------------------
# Step 1 -- sweep every marketable item at data-center / region level
# ----------------------------------------------------------------------------

def sweep():
    print("[1/4] Sweeping every marketable item")
    dcs = F.get("https://universalis.app/api/v2/data-centers") or []
    dc = next((d for d in dcs if d["name"] == SELL_DC), None)
    if not dc:
        sys.exit(f"Could not find data center {SELL_DC!r} in the Universalis list.")
    worlds_all = {w["id"]: w["name"] for w in (F.get("https://universalis.app/api/v2/worlds") or [])}
    sell_worlds = {wid: worlds_all[wid] for wid in dc["worlds"] if wid in worlds_all}
    print(f"  {SELL_DC} worlds: {', '.join(sorted(sell_worlds.values()))}")

    items = F.get("https://universalis.app/api/v2/marketable") or []
    print(f"  {len(items):,} marketable items")
    batches = chunks(items, 100)

    def pull(arg):
        scope, batch = arg
        ids = ",".join(map(str, batch))
        return F.get(f"https://universalis.app/api/v2/aggregated/{scope}/{ids}")

    agg = {}
    for scope, key in ((SELL_DC, "sell"), (BUY_PROBE_DC, "buy")):
        results = F.map(pull, [(scope, b) for b in batches], f"{key} side")
        table = {}
        for res in results:
            for row in (res or {}).get("results", []):
                table[row["itemId"]] = row
        agg[key] = table
    return agg, sell_worlds, worlds_all


# ----------------------------------------------------------------------------
# Step 2 -- shortlist candidates from the cheap aggregate data
# ----------------------------------------------------------------------------

def dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
        if obj is None:
            return None
    return obj


def shortlist(agg):
    print("[2/4] Shortlisting candidates")
    now = time.time()
    rows = []
    for sid, sell in agg["sell"].items():
        buy = agg["buy"].get(sid)
        if not buy:
            continue
        for q in ("nq", "hq"):
            buy_price = dig(buy, q, "minListing", "region", "price")
            board = dig(sell, q, "minListing", "dc", "price")
            avg = dig(sell, q, "averageSalePrice", "dc", "price")
            vel = dig(sell, q, "dailySaleVelocity", "dc", "quantity") or 0.0
            last = dig(sell, q, "recentPurchase", "dc", "timestamp")
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

    big = sorted([r for r in rows if r["profit"] >= BIG_TICKET_FLOOR],
                 key=lambda r: -r["score"])[:CANDIDATES_PER_TIER]
    bulk = sorted([r for r in rows if r["profit"] < BIG_TICKET_FLOOR],
                  key=lambda r: -r["score"])[:CANDIDATES_PER_TIER]
    ids = sorted({r["id"] for r in big + bulk})
    print(f"  {len(rows):,} priced routes -> {len(big)} big-ticket + {len(bulk)} bulk"
          f" ({len(ids)} items to inspect)")
    return big, bulk, ids


# ----------------------------------------------------------------------------
# Step 3 -- deep fetch: real listings and sale history for the shortlist
# ----------------------------------------------------------------------------

def deep_fetch(ids):
    print("[3/4] Pulling listings and sale history")
    names = {}
    for batch in chunks(ids, 100):
        res = F.get("https://v2.xivapi.com/api/sheet/Item?rows="
                    + ",".join(map(str, batch))
                    + "&fields=Name,StackSize,ItemUICategory.Name")
        for row in (res or {}).get("rows", []):
            f = row["fields"]
            cat = f.get("ItemUICategory") or {}
            cat_name = (cat.get("fields") or {}).get("Name") or ""
            names[row["row_id"]] = {"n": f["Name"], "s": f.get("StackSize") or 1,
                                    "c": cat_name}
    print(f"  named {len(names)} items")

    def pull(arg):
        scope, iid = arg
        tail = "?listings=100&entries=200" if scope == SELL_DC else "?listings=100&entries=0"
        return scope, iid, F.get(f"https://universalis.app/api/v2/{scope}/{iid}{tail}")

    jobs = [(s, i) for i in ids for s in (SELL_DC, BUY_SCOPE)]
    data = {}
    for scope, iid, payload in F.map(pull, jobs, "market data"):
        if payload:
            data[f"{scope}|{iid}"] = payload
    return names, data


# ----------------------------------------------------------------------------
# Step 4 -- per-world economics
# ----------------------------------------------------------------------------

def buy_side(data, iid, hq, worlds_all):
    """Cheapest NA source, plus the single world holding the most of it."""
    payload = data.get(f"{BUY_SCOPE}|{iid}")
    if not payload:
        return None
    listings = sorted([l for l in payload["listings"] if bool(l.get("hq")) == hq],
                      key=lambda l: l["pricePerUnit"])
    if not listings:
        return None
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
    return {"min": floor, "stop": worlds_all.get(wid, "?"), "stop_units": units,
            "buy": spend / units, "region_units": sum(l["quantity"] for l in cheap)}


def sell_side(data, iid, hq, sell_worlds):
    """Per-world board state and demand rate on the selling data center."""
    payload = data.get(f"{SELL_DC}|{iid}")
    if not payload:
        return None
    listings = [l for l in payload["listings"] if bool(l.get("hq")) == hq]
    history = [h for h in payload["recentHistory"] if bool(h.get("hq")) == hq]
    if len(history) < MIN_DC_SALES:
        return None

    stamps = [h["timestamp"] for h in history]
    # One observation window shared by every world, so a short burst on one
    # world can't be mistaken for a permanently high rate.
    window = max((max(stamps) - min(stamps)) / 86400.0, 0.5)
    dc_median = st.median([h["pricePerUnit"] for h in history])

    per = {}
    for wid, wname in sell_worlds.items():
        w_list = sorted([l for l in listings if l.get("worldID") == wid],
                        key=lambda l: l["pricePerUnit"])
        w_hist = [h for h in history if h.get("worldID") == wid]
        units = sum(h["quantity"] for h in w_hist)
        prices = [h["pricePerUnit"] for h in w_hist]
        refs = [p for p in (w_list[0]["pricePerUnit"] if w_list else None,
                            st.median(prices) if prices else None,
                            dc_median * PRICE_SANITY) if p]
        sell = min(refs)
        per[wname] = {
            "sell": round(sell),
            "ahead": sum(l["quantity"] for l in w_list if l["pricePerUnit"] <= sell),
            "listed": sum(l["quantity"] for l in w_list),
            "rate": units / window,
            "sold": units,
            "sales": len(w_hist),
        }
    return per, round(window, 1)


def analyse(tiers, names, data, sell_worlds, worlds_all):
    print("[4/4] Working out per-world economics")
    out = {}
    for tier, cands in tiers.items():
        rows = []
        for c in cands:
            iid, hq = c["id"], c["hq"]
            meta = names.get(iid)
            if not meta:
                continue
            buy = buy_side(data, iid, hq, worlds_all)
            sell = sell_side(data, iid, hq, sell_worlds)
            if not buy or not sell:
                continue
            per, window = sell
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
                throughput = min(v["rate"], stack)
                worlds[wname] = {
                    "s": v["sell"], "p": round(profit), "m": round(margin * 100),
                    "a": v["ahead"], "l": v["listed"], "r": round(v["rate"], 2),
                    "so": v["sold"], "ns": v["sales"],
                    "g": round(profit * throughput),
                    "d": round(1 / v["rate"], 1) if v["rate"] > 0 else None,
                }
            if not worlds:
                continue
            rows.append({"n": meta["n"], "c": meta["c"], "q": "HQ" if hq else "NQ",
                         "st": stack, "b": round(buy["buy"]), "stop": buy["stop"],
                         "su": buy["stop_units"], "nau": buy["region_units"],
                         "win": window, "w": worlds})
        rows.sort(key=lambda r: -max(v["g"] for v in r["w"].values()))
        out[tier] = rows
        print(f"  {tier}: {len(rows)} tradeable routes")
    return out


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------

def render(tables, sell_worlds, counts):
    if not os.path.exists(TEMPLATE):
        sys.exit(f"Missing {TEMPLATE}. It should sit next to this script.")
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    payload = {
        "big": tables["big"][:ROWS_PER_TIER],
        "bulk": tables["bulk"][:ROWS_PER_TIER],
        "meta": {
            "gen": datetime.now().strftime("%d %b %Y, %H:%M"),
            "worlds": sorted(sell_worlds.values()),
            "nbig": counts["big"], "nbulk": counts["bulk"],
            "dc": SELL_DC, "region": BUY_SCOPE.replace("-", " "),
        },
    }
    html = html.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    return OUT


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="FFXIV NA -> Materia arbitrage scanner")
    ap.add_argument("--offline", action="store_true",
                    help="re-render from the last cached fetch instead of hitting the API")
    ap.add_argument("--top", type=int, default=ROWS_PER_TIER,
                    help=f"rows to keep per tier (default {ROWS_PER_TIER})")
    ap.add_argument("--no-open", action="store_true", help="don't open the page when done")
    args = ap.parse_args()

    global ROWS_PER_TIER
    ROWS_PER_TIER = args.top
    os.makedirs(CACHE, exist_ok=True)
    snapshot = os.path.join(CACHE, "snapshot.json")
    started = time.time()

    if args.offline:
        if not os.path.exists(snapshot):
            sys.exit("No cached scan yet -- run once without --offline first.")
        with open(snapshot, encoding="utf-8") as fh:
            snap = json.load(fh)
        print(f"Re-rendering from cache ({snap['stamp']})")
        names = {int(k): v for k, v in snap["names"].items()}
        worlds_all = {int(k): v for k, v in snap["worlds_all"].items()}
        sell_worlds = {int(k): v for k, v in snap["sell_worlds"].items()}
        tiers = {"big": snap["big"], "bulk": snap["bulk"]}
        tables = analyse(tiers, names, snap["data"], sell_worlds, worlds_all)
    else:
        agg, sell_worlds, worlds_all = sweep()
        big, bulk, ids = shortlist(agg)
        names, data = deep_fetch(ids)
        tiers = {"big": big, "bulk": bulk}
        tables = analyse(tiers, names, data, sell_worlds, worlds_all)
        with open(snapshot, "w", encoding="utf-8") as fh:
            json.dump({"stamp": datetime.now().strftime("%d %b %Y, %H:%M"),
                       "names": {str(k): v for k, v in names.items()},
                       "worlds_all": {str(k): v for k, v in worlds_all.items()},
                       "sell_worlds": {str(k): v for k, v in sell_worlds.items()},
                       "big": big, "bulk": bulk, "data": data}, fh)

    counts = {"big": len(tables["big"]), "bulk": len(tables["bulk"])}
    path = render(tables, sell_worlds, counts)
    print(f"\nWrote {path}  ({time.time() - started:.0f}s"
          + (f", {F.fails} requests failed" if F.fails else "") + ")")
    if not args.no_open:
        webbrowser.open("file:///" + path.replace("\\", "/"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
