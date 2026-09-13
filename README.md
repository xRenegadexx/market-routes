# FFXIV Market Routes

Finds items worth buying on one side of the Materia / North America divide and
selling on the other, using live market board data from
[Universalis](https://universalis.app).

**[Download the latest version](../../releases/latest)** — one file, nothing to
install.

Windows will warn you the first time ("Windows protected your PC") because the
app isn't code-signed: *More info* → *Run anyway*. Some antivirus flags it for
the same reason.

Put it somewhere writable such as Desktop or Downloads rather than Program
Files. It keeps its data in a folder beside itself and replaces itself in place
when it updates.

The first launch scans for about five minutes before there's anything to see.
After that it opens instantly on the last scan and checks for new versions on
its own.

## What the numbers mean

Both trade directions are lopsided in the same way and that shapes every column:

- **Buying is unrestricted.** You can travel across a whole region so the buy
  price is the cheapest listing anywhere on that side. *Best stop* names the one
  world holding the most stock at that price so you make one trip instead of
  four.
- **Selling is fixed to one board.** Market boards are per-world so your only
  competition is what's listed on your seller's home world. *Ahead* counts units
  already listed at or below your price there.

Profits are net of the 5% market board tax.

**Trip value** is what one shopping run is actually worth: profit per unit times
however many units you can really buy. **Gil / slot / day** is the sustained
rate instead — retainer listing slots rather than gil are what you run out of
and one slot holds one listing of up to a full stack. The two disagree when
stock is thin and trip value is usually the honest one.

**Sale rates** come from up to 200 recent sales per item per data centre,
bucketed by world and divided by the window those sales span. Each data centre
is measured on its own clock so a quiet world isn't judged against a busy one
and a short burst isn't read as a permanent rate.

## What it throws out

Crowd-sourced data is noisy so the tables exclude sell prices above 1.5x the
data-centre median sale, margins over 1500% (private trades rather than demand),
items with fewer than four recorded sales and anything that hasn't sold in a
week. The buy floor also ignores listings under a quarter of the median so one
person dumping a unit far below market can't delete an item from the tables.

Those filters are why an item you expect can be missing. **Look up an item**
applies none of them — use it when something you're watching disappears.

## The tabs

| | |
|---|---|
| **Big ticket** | 20,000 gil or more per unit, bought anywhere in NA |
| **Bulk & consumables** | under 20,000 — thin per unit but a slot holds a stack |
| **Materia → NA** | the other direction |
| **My listings** | your own listings, found by retainer name |
| **Look up an item** | price one item on every world with no filters applied |
| **Shopping list** | what you've ticked, grouped by where to buy it |

Each route tab has its own world picker because each sells somewhere different.
Set your retainer names under **Settings** and the app picks your own listings
out of any board and tells you how far you've been undercut.

## Keyboard

| | |
|---|---|
| `Ctrl+R` | full refresh |
| `F5` | re-price just the selected item |
| `Ctrl+F` | filter box |
| `[` `]` | move the sort column, `Enter` reverses |
| double-click | open the board behind a row |
| `Esc` | stop a running scan |

## Privacy

Three hosts over HTTPS only and it refuses any other URL or redirect:
`universalis.app` for market data, `v2.xivapi.com` for item names and GitHub
only when you check for updates. No account, no telemetry, nothing collected.
Retainer names from listings are stored locally so your own rows can be marked;
delete the `data` folder any time and it rebuilds.

## Caveats

Estimates from crowd-sourced data rather than guarantees. Prices move, a row
backed by four sales is a guess and the figures assume you hold the lowest price
long enough to capture that world's sales. If something looks wrong, `error.log`
in the data folder records crashes with the version number.

---

## Releasing (maintainer)

```bash
python release.py 1.9.4 "What changed"
```

Bumps the version, builds, commits, pushes and publishes the release with the
exe attached. Tags need three numbers — `v1.9.4` works, `v1.9` parses as `0.0.0`
and nobody is ever offered it.

Requires the [GitHub CLI](https://cli.github.com) logged in from a
**non-elevated** prompt — an elevated one can't read the saved token.

Unofficial tool, not affiliated with or endorsed by Square Enix.
FINAL FANTASY XIV © SQUARE ENIX CO., LTD.
