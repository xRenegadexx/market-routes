#!/usr/bin/env python3
"""
Small local store for things the app remembers between runs: your shopping
list, and your settings.

Both live in .cache/ as plain JSON. Nothing here leaves the machine, and a
corrupt or hand-edited file falls back to empty defaults rather than stopping
the app from opening.
"""

import json
import os

import paths

CACHE = paths.DATA_DIR
BASKET = os.path.join(CACHE, "basket.json")
SETTINGS = os.path.join(CACHE, "settings.json")
POSITIONS = os.path.join(CACHE, "positions.json")
THEME = os.path.join(CACHE, "theme.json")

DEFAULTS = {
    "auto_refresh": False,       # re-scan on its own once the data goes stale
    "auto_refresh_hours": 12,    # how old "stale" means
    "refresh_on_open": False,    # scan at launch if the data is already stale
    "budget_gil": 0,             # 0 = don't track
    "affordable_only": False,    # hide rows a run couldn't pay for
    "uncontested_only": False,   # hide rows with sellers under you
    "min_margin": 0,             # percent; 0 = no floor
    "retainer_slots": 40,        # two free retainers, 20 listings each
    "worlds": {},                # tier -> last chosen world
    # Your retainers, as {"name": ..., "world": ...}. The world matters: retainer
    # names are only unique within a world, so two people on different servers
    # can both have a "Kupo" and matching on name alone would claim theirs.
    "retainers": [],
}


def _read(path, fallback):
    # utf-8-sig: Notepad and PowerShell write a BOM, and a BOM read as
    # plain utf-8 raises -- which would silently reset your settings.
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, type(fallback)) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def _write(path, data):
    os.makedirs(CACHE, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass          # a read-only disk shouldn't take the window down


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

def load_settings():
    saved = _read(SETTINGS, {})
    out = dict(DEFAULTS)
    for k, v in saved.items():
        if k in out and isinstance(v, type(out[k])):
            out[k] = v
    return out


def save_settings(settings):
    _write(SETTINGS, {k: settings.get(k, v) for k, v in DEFAULTS.items()})


# ----------------------------------------------------------------------------
# Shopping list
# ----------------------------------------------------------------------------
# One entry per item + quality + world you plan to sell it on, so the same item
# bought for two different worlds stays two lines.

def key_of(row, tier, world):
    return f"{tier}|{row['id']}|{row['q']}|{world}"


def load_basket():
    return _read(BASKET, {})


def save_basket(basket):
    _write(BASKET, basket)


def toggle(basket, row, tier, world):
    """Add the row, or drop it if it's already there. Returns True if added."""
    k = key_of(row, tier, world)
    if k in basket:
        del basket[k]
        return False
    basket[k] = {
        "tier": tier, "id": row["id"], "name": row["n"], "q": row["q"],
        "world": world, "stop": row["stop"], "buy": row["b"],
        "sell": row["s"], "profit": row["p"], "qty": row["u"],
        "stack": row["st"],
    }
    return True


# These files are plain JSON on disk, so they can be hand-edited, truncated, or
# left over from an older build. Read every field defensively: one unreadable
# line should cost you that line, not the window.

def _num(row, key, default=0):
    v = row.get(key, default) if isinstance(row, dict) else default
    return v if isinstance(v, (int, float)) else default


def usable(row):
    """A basket line we can do arithmetic on."""
    return isinstance(row, dict) and _num(row, "qty") > 0


def totals(basket, entries=None):
    """Capital, return and slot count for a basket, or a subset of it."""
    source = entries if entries is not None else basket.values()
    rows = [r for r in source if usable(r)]
    spend = sum(_num(r, "buy") * _num(r, "qty") for r in rows)
    gain = sum(_num(r, "profit") * _num(r, "qty") for r in rows)
    # One retainer listing per line, but a quantity larger than one stack needs
    # more than one listing.
    slots = sum(max(1, -(-int(_num(r, "qty")) // max(int(_num(r, "stack", 1)), 1)))
                for r in rows)
    return {"lines": len(rows), "spend": spend, "gain": gain, "slots": slots}


def by_stop(basket):
    """Group the list by the world you'd buy it on, so a run is one trip each."""
    def value(r):
        return _num(r, "profit") * _num(r, "qty")

    groups = {}
    for r in basket.values():
        if usable(r):
            groups.setdefault(r.get("stop") or "unknown", []).append(r)
    for rows in groups.values():
        rows.sort(key=lambda r: -value(r))
    return dict(sorted(groups.items(),
                       key=lambda kv: -sum(value(r) for r in kv[1])))


# ----------------------------------------------------------------------------
# Positions -- what you've actually listed, so later checks can spot undercuts
# ----------------------------------------------------------------------------

def load_positions():
    return _read(POSITIONS, {})


def save_positions(positions):
    _write(POSITIONS, positions)


def board_key(item_id, quality, world):
    """Identifies a market board: one item, one quality, one world."""
    return f"{item_id}|{quality}|{world}"


def open_position(positions, row, world, price, qty):
    """Record ONE listing: `qty` units at `price` on `world`.

    The game puts each stack on the board as its own listing at its own price --
    six stacks of 99 is six listings, not one of 594 -- so each gets its own
    entry here. Keying only by item and world (as this once did) meant recording
    a second stack silently replaced the first.
    """
    import time
    stamp = int(time.time() * 1000)
    key = f"{board_key(row['id'], row['q'], world)}|{stamp}"
    while key in positions:          # two in the same millisecond
        stamp += 1
        key = f"{board_key(row['id'], row['q'], world)}|{stamp}"
    positions[key] = {
        "id": row["id"], "name": row["n"], "q": row["q"], "world": world,
        "price": int(price), "qty": int(qty), "cost": row["b"],
        "at": stamp, "state": "listed", "note": "not checked yet",
    }
    return key


def migrate_positions(positions):
    """Bring pre-1.2 entries onto the new key shape. Harmless to re-run."""
    changed = False
    for key in list(positions):
        if key.count("|") == 2:      # old item|quality|world key
            pos = positions.pop(key)
            at = pos.get("at") or 0
            positions[f"{key}|{at}"] = pos
            changed = True
    return changed


def positions_by_board(positions):
    """Group listings by the board they sit on, for checking and display."""
    groups = {}
    for key, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        groups.setdefault(board_key(pos.get("id"), pos.get("q"),
                                    pos.get("world")), []).append((key, pos))
    for rows in groups.values():
        rows.sort(key=lambda kv: kv[1].get("price", 0))
    return groups


def close_position(positions, key):
    positions.pop(key, None)


# ----------------------------------------------------------------------------
# Colours
# ----------------------------------------------------------------------------
# Named roles rather than raw names, so a change stays coherent: "ground" is
# behind everything, "surface" is panels and tables, "accent" is the one colour
# doing the pointing. Semantic colours stay separate from the accent -- good and
# bad must not be things you can accidentally set to the same hue as a heading.

ROLES = [
    ("ground",   "Window background",  "#11141d"),
    ("surface",  "Panels and tables",  "#191d29"),
    ("surface2", "Alternating rows",   "#1f2431"),
    ("line",     "Borders",            "#2b3141"),
    ("ink",      "Main text",          "#e8eaf0"),
    ("ink2",     "Secondary text",     "#a8b0c0"),
    ("ink3",     "Labels and hints",   "#8892a6"),
    ("accent",   "Accent / highlights", "#d0a24a"),
    ("good",     "Positive",           "#59b8a6"),
    ("warn",     "Warnings",           "#dd8a71"),
]

PRESETS = {
    "Dark (default)": {k: v for k, _l, v in ROLES},
    "Midnight": {
        "ground": "#0b0f14", "surface": "#121821", "surface2": "#18202b",
        "line": "#22303d", "ink": "#e6edf3", "ink2": "#9fb0c0", "ink3": "#7d919f",
        "accent": "#4bb3fd", "good": "#4ec9a5", "warn": "#e0776a",
    },
    "Parchment": {
        "ground": "#eceae3", "surface": "#ffffff", "surface2": "#f5f3ec",
        "line": "#d8d3c6", "ink": "#23211c", "ink2": "#545046",
        "ink3": "#6f695c", "accent": "#9c6f16", "good": "#2f7a6d",
        "warn": "#a8503a",
    },
    "Slate": {
        "ground": "#1b1d21", "surface": "#24272c", "surface2": "#2c3037",
        "line": "#3a3f47", "ink": "#eceef1", "ink2": "#b3b9c2", "ink3": "#8c939d",
        "accent": "#c9a227", "good": "#5fb98f", "warn": "#d98b6a",
    },
}


def valid_hex(value):
    if not isinstance(value, str):
        return False
    v = value.strip()
    return (len(v) == 7 and v[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in v[1:]))


def load_theme():
    """Saved colours, filled in from the default for anything missing."""
    saved = _read(THEME, {})
    theme = {key: default for key, _label, default in ROLES}
    for key, value in (saved.items() if isinstance(saved, dict) else []):
        if key in theme and valid_hex(value):
            theme[key] = value.strip()
    return theme


def save_theme(theme):
    _write(THEME, {k: v for k, v in theme.items()
                   if k in dict((r[0], r[2]) for r in ROLES) and valid_hex(v)})


def reset_theme():
    try:
        if os.path.exists(THEME):
            os.remove(THEME)
    except OSError:
        pass


def clean_retainers(raw):
    """Normalise the saved retainer list, upgrading the old name-only format."""
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            # Pre-1.4 saved bare names with no world. Keep the name so it isn't
            # lost, but leave the world blank -- the app asks for it.
            name, world = entry.strip(), ""
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            world = str(entry.get("world") or "").strip()
        else:
            continue
        if name and not any(r["name"].lower() == name.lower()
                            and r["world"].lower() == world.lower() for r in out):
            out.append({"name": name, "world": world})
    return out


def retainers_on(retainers, world):
    """Your retainer names on one world, lowercased for matching.

    An entry with no world set (carried over from the old format) is matched
    everywhere, so upgrading doesn't silently stop marking your listings.
    """
    target = (world or "").strip().lower()
    return {r["name"].lower() for r in clean_retainers(retainers)
            if not r["world"] or r["world"].lower() == target}
