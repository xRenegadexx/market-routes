# FFXIV Market Routes

A desktop app that finds items worth buying on one side of the Materia / North
America divide and selling on the other. Pick the world your seller character
lives on and every number re-prices against that world's board.

## Running it

Double-click `run.bat`, or:

```bash
python app.py
```

Nothing to install — standard library only. Python 3.9 or newer.

The window opens on the last scan's data straight away. **Refresh data** pulls a
fresh one, which takes several minutes and shows progress as it goes. You can keep
reading the old data while it runs, and **Stop** aborts it.

For a refresh with no window (e.g. from Task Scheduler):

```bash
python engine.py
```

## The three tabs

| Tab | Buy | Sell |
|---|---|---|
| **Big ticket** | anywhere in NA | one Materia world |
| **Bulk & consumables** | anywhere in NA | one Materia world |
| **Materia → NA** | anywhere on Materia | one NA world |

Each tab has its own world picker, because each sells somewhere different — five
buttons for Materia, two dropdowns (data center, then world) for the thirty-two
NA worlds.

## What it is actually measuring

Both directions are asymmetric in the same way, and that shapes every column:

- **Buying is unrestricted.** You can travel across a whole region, so the buy
  price is the cheapest listing anywhere on that side. *Best stop* names the one
  world holding the most stock at that price, so you can do a single trip instead
  of four.
- **Selling is fixed to one board.** Market boards are per-world, so your only
  competition is what's listed on your seller's home world. *Ahead* counts units
  already listed at or below your price there.

Profits are net of the 5% market board tax the seller pays.

### Gil / slot / day

The column the tables sort by. Retainer listing slots, not gil, are what you run
out of — one slot holds one listing of up to a full stack. So a slot is worth
`profit per unit × min(stack size, that world's sales per day)`. It's the only
fair way to compare one 38-million-gil hairstyle against a stack of gemdraughts.

### Where the sale rate comes from

Up to 200 recent sales per item **per data center**, bucketed by world and divided
by the window those sales span. Each DC is measured on its own clock, so a world in
a quiet DC isn't judged against a busy one, and a short burst can't be read as a
permanent rate. `Sells in` is how long one unit takes at that rate.

## Guardrails on the numbers

Crowd-sourced market data is noisy, so the scan throws out:

- Sell prices above **1.5×** the data-center median sale, so one whale listing
  can't inflate a row
- Margins above **1500%**, which are private trades rather than real demand
- Items with fewer than **4** recorded sales, where there's nothing to judge
- Anything that hasn't sold on the selling side in the last **7 days**

All tunable at the top of `engine.py`, along with the 20,000 gil line dividing the
first two tabs.

## When Universalis is down

The app checks on startup and shows an orange banner if it can't get through,
telling you whether it's your connection or the API, with a **Check again** button.
Your last scan stays on screen — the banner says which scan you're looking at. A
refresh that fails the same way leaves the old data alone rather than blanking it.

## Keyboard

| | |
|---|---|
| `Ctrl+R` | refresh |
| `Ctrl+F` | jump to the filter box |
| `[` `]` | move the sort column |
| `Enter` | reverse the sort |
| `Space` / double-click | open the item on Universalis |
| `Esc` | stop a running scan |

---

## Putting it on GitHub

You only do this once. It gives you a backup, and it's what the update button
reads from.

**1. Make the repository.** Go to <https://github.com/new>. Name it `ffxiv-arb`,
leave it **Public**, and do *not* tick "Add a README" — this folder already has one.
Click *Create repository*.

> Public matters for the update button: reading a private repo needs an access
> token, which this tool deliberately doesn't handle. If you'd rather keep it
> private, that's fine — just update with `git pull` instead of the button.

**2. Push this folder.** In a terminal here:

```bash
git remote add origin https://github.com/YOUR-USERNAME/ffxiv-arb.git
git branch -M main
git push -u origin main
```

Git will ask you to sign in to GitHub the first time; a browser window handles it.

**3. That's it.** The update button now works, because the app reads the repo name
straight out of `.git/config`.

### Changing something later

```bash
git add -A
git commit -m "Describe what you changed"
git push
```

## The update button

**Check for updates** asks GitHub for the newest commit in your repo. If it's
different from what's installed, a banner offers to **Install**: the app downloads
`app.py`, `engine.py`, `update.py`, `run.bat` and `README.md` at that commit,
refuses any Python file that doesn't compile, copies the current version into
`.cache/backup`, and swaps them in. Restart to run it.

`python update.py` does the same from a terminal, and `update.restore()` puts the
backup back.

**Understand what this is.** An update replaces the code this app runs. Whoever can
push to that repository decides what runs on your machine next time you start it —
fine for your own repo, not something to point at a stranger's. Nothing happens
silently: checking is one click, installing is a second, and a confirmation dialog
sits in between.

If you didn't clone from GitHub, put the repo in `config.json` next to `app.py`:

```json
{"repo": "YOUR-USERNAME/ffxiv-arb"}
```

## Files

| | |
|---|---|
| `app.py` | the window |
| `engine.py` | scanning and analysis; runnable on its own |
| `update.py` | the GitHub updater; runnable on its own |
| `.cache/` | last scan, backups, installed-version stamp — never committed |

## Privacy and network

It talks to three hosts, over HTTPS only, and refuses any URL or redirect that
isn't one of them:

- `universalis.app` — market listings and sale history
- `v2.xivapi.com` — item names, stack sizes, categories
- `api.github.com` / `raw.githubusercontent.com` — only when you press update

No account, no API key, no telemetry, no analytics. Nothing about you is collected
or transmitted. The cache holds prices and quantities only — the retainer names and
seller IDs in the raw API responses are stripped before anything is written to disk.
Delete `.cache/` any time; it rebuilds on the next refresh.

## Caveats

These are estimates from crowd-sourced data, not guarantees. Prices move, a row
backed by four sales is a guess rather than a forecast, and the figures assume you
hold the lowest price long enough to capture that world's sales. Nothing here
accounts for your travel time.

Unofficial tool, not affiliated with or endorsed by Square Enix.
FINAL FANTASY XIV © SQUARE ENIX CO., LTD.
