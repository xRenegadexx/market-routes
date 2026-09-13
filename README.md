# FFXIV Market Routes

A desktop app that finds items worth buying on one side of the Materia / North
America divide and selling on the other. Pick the world your seller character
lives on and every number re-prices against that world's board.

## Running it

**Just want to use it:** download `FFXIV Market Routes.exe` and double-click it.
Nothing else to install — no Python, no setup. Windows 64-bit.

Two things to expect the first time:

- **"Windows protected your PC."** The exe isn't code-signed (that needs a paid
  certificate), so SmartScreen warns about it. Click *More info* → *Run anyway*.
- **Your antivirus may grumble.** Apps built this way share a bootloader with
  some malware, so scanners flag them by association. It's a false positive, and
  you can check for yourself: the source is right here in this repo.

It opens with no data. Press **Refresh data** and give it a few minutes — it's
reading every marketable item on both sides. After that it opens instantly on the
last scan, and refreshing is something you do when you feel like it.

The exe keeps its scans and settings in a `data` folder next to itself, so put it
somewhere it can write — your Desktop or Downloads is fine, Program Files isn't.
If the folder beside it is read-only it falls back to your local app data.

### Running from source instead

Needs Python 3.9+ with tkinter. Double-click `run.bat` (it checks your setup and
tells you what's missing), or:

```bash
python app.py
```

For a refresh with no window (e.g. from Task Scheduler):

```bash
python engine.py
```

### Building the exe yourself

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name "FFXIV Market Routes" app.py
```

The result lands in `dist/`.

## The tabs

| Tab | Buy | Sell |
|---|---|---|
| **Big ticket** | anywhere in NA | one Materia world |
| **Bulk & consumables** | anywhere in NA | one Materia world |
| **Materia → NA** | anywhere on Materia | one NA world |
| **Shopping list** | what you've ticked, grouped by where to buy it |
| **My listings** | what you've listed, and whether anyone's under you |

The three route tabs each have their own world picker, because each sells
somewhere different — five buttons for Materia, two dropdowns (data center, then
world) for the thirty-two NA worlds.

Note that "big ticket" means high profit *per unit*, not expensive: most of those
rows cost well under 500k gil to run.

## Narrowing it down

Text filter aside, there are three switches above the table:

- **Only what I can afford** — hides anything whose buy price × buy quantity
  exceeds the gil you entered on the shopping list tab
- **Uncontested only** — hides rows where somebody is already listed under your
  price on your world
- **Min margin %** — a floor on the return

A note beside them says how many rows are being hidden, so a filter you forgot
about can't quietly empty the table.

## The shopping list

Click the ✓ column on any row to add it. The shopping list tab groups everything
by the world you'd buy it on, so each heading is one trip, and totals the run
against two budgets: the gil you have, and your free retainer slots. Either going
over turns orange. **Copy as text** puts the whole list on the clipboard.

The list survives between sessions.

## Watching your listings

Open any row (double-click) and the detail panel shows the actual wall on your
world — every price and quantity — beside what the item has really been selling
for, plotted over time with your intended price marked. A tall wall at a single
price looks very different from a real market, which is the tell for a price one
seller is propping up.

**I listed this** records what you actually put up, and the **My listings** tab
then tracks it. **Check my listings** queries only those items on only those
worlds, so it takes seconds rather than the minutes a full scan needs, and tells
you when somebody has gone under you.

It cannot tell you an item *sold*. Listings carry no identity that could be
matched against yours, so the app reports where your price sits on the board and
leaves the conclusion to you.

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

### The change column

The last eight scans are kept as a compact digest — profit and sell price per item
per world, not whole snapshots, which run to tens of megabytes each. The **Change**
column shows how a margin moved since the previous scan, so you can see a spread
closing before you buy into it. It stays blank until you have two scans.

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
| double-click | open the item detail panel |
| `Space` | open the item on Universalis |
| `+` | add the selected row to the shopping list |
| `Esc` | stop a running scan |

---

## Releasing a new version

Testers run the .exe and update from inside the app. That works off **GitHub
Releases**, not commits: the app asks for the newest release, compares its tag
to its own version, and downloads the .exe attached to it.

So a release is three steps:

**1. Bump the version.** In `paths.py`:

```python
VERSION = "1.8.0"
```

**2. Build.**

```bash
python -m PyInstaller --onefile --windowed --clean --noconfirm --name "FFXIV Market Routes" --icon icon.ico --add-data "icon.ico;." app.py
```

**3. Publish it.** Push your changes, then on GitHub go to *Releases* → *Draft a
new release*, tag it **`v1.8.0`** (matching the version you just set), attach
`dist/FFXIV Market Routes.exe`, and publish.

That's it. Every tester's copy will offer the update next time they press the
button.

### What the tag has to look like

The app compares version numbers, so the tag needs three numbers in it: `v1.8.0`
or `1.8.0` both work, `v1.8` does not. A tag that doesn't parse sorts as
`0.0.0`, which means nobody is ever offered the update — if a release seems
invisible, check the tag first.

The release must also have an `.exe` attached. A release with no attachment
produces a clear message rather than a silent failure, but it still can't
install anything.

### How updating actually works

Windows won't let a running program be overwritten, but it will let one be
renamed. So the app downloads the new exe beside itself, renames the running one
to `previous-version.exe`, and gives its name to the new one. On the next start
the old copy is deleted.

That leaves two useful properties: a download that fails or isn't a real program
changes nothing, and if a new build misbehaves the previous one is sitting right
there to rename back — until you restart, at which point it's cleaned up.

Scans, shopping lists, tracked listings and colours live in the `data` folder and
are never touched by an update.

## Putting it on GitHub the first time

**1. Make the repository.** <https://github.com/new>, named `market-routes`,
**Public**, and don't tick "Add a README" — this folder has one.

> Public matters: reading a private repo needs an access token, which this tool
> deliberately doesn't handle.

**2. Push.**

```bash
git remote add origin https://github.com/xRenegadexx/market-routes.git
git branch -M main
git push -u origin main
```

Git will ask you to sign in the first time; a browser window handles it.

### Changing something later

```bash
git add -A
git commit -m "Describe what you changed"
git push
```

Remember that pushing alone doesn't update anyone — testers get new versions from
Releases, so follow the three steps above when you want them to.

## Files

| | |
|---|---|
| `app.py` | the window |
| `engine.py` | scanning and analysis; runnable on its own |
| `store.py` | shopping list, tracked listings and settings |
| `paths.py` | works out where to keep data, source build or exe |
| `update.py` | the GitHub updater; runnable on its own |
| `.cache/` | scans, trend history, your list — never committed |

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

## If it won't start

There's no console behind the window, so a crash used to be silent. It isn't now:
anything unhandled gets written to `error.log` (in the `data` folder beside the
exe, or `.cache/` when running from source) and shown in a dialog. If you hit one,
that file says what happened.

## Caveats

These are estimates from crowd-sourced data, not guarantees. Prices move, a row
backed by four sales is a guess rather than a forecast, and the figures assume you
hold the lowest price long enough to capture that world's sales. Nothing here
accounts for your travel time.

Unofficial tool, not affiliated with or endorsed by Square Enix.
FINAL FANTASY XIV © SQUARE ENIX CO., LTD.
