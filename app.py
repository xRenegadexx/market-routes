#!/usr/bin/env python3
"""
Desktop window for the FFXIV Materia <-> NA arbitrage scanner.

Each tab sells in a different place, so each tab has its own world picker: the
NA -> Materia tabs pick one of the five Materia worlds, and the Materia -> NA tab
picks one of the thirty-two NA worlds. Market boards are per-world, so the world
you choose is what the sell price, the competition and the sale rate are measured
against.

Everything runs locally against the Universalis API; no account or key needed.

Run:  python app.py      (or double-click run.bat on Windows)
"""

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import engine
import update

# ----------------------------------------------------------------------------
# Look
# ----------------------------------------------------------------------------

BG = "#11141d"
PANEL = "#191d29"
PANEL_2 = "#1f2431"
LINE = "#2b3141"
INK = "#e8eaf0"
INK_2 = "#a8b0c0"
INK_3 = "#8892a6"   # 4.95:1 on the table header fill -- WCAG AA at this size
GOLD = "#d0a24a"
GOOD = "#59b8a6"
WARN = "#dd8a71"
ALERT_BG = "#2a1d18"

TIER_ORDER = ["big", "bulk", "rev"]

# key, heading, width, anchor, kind
COLUMNS = [
    ("n",    "Item",             240, "w", "text"),
    ("q",    "Q",                 34, "c", "text"),
    ("b",    "Buy",               88, "e", "gil"),
    ("stop", "Best stop",        140, "w", "text"),
    ("s",    "Sell price",        92, "e", "gil"),
    ("p",    "Net / unit",        92, "e", "gil"),
    ("m",    "Margin",            66, "e", "pct"),
    ("a",    "Ahead",             58, "e", "int"),
    ("r",    "Sells / day",       82, "e", "rate"),
    ("d",    "Sells in",          74, "e", "dur"),
    ("t",    "Trip value",       100, "e", "gil"),
    ("u",    "Buy qty",           70, "e", "int"),
    ("g",    "Gil / slot / day", 106, "e", "gil"),
    ("nau",  "Stock",             68, "e", "int"),
]

BLURB = {
    "big": "Items netting 20,000 gil or more per unit, bought anywhere in North "
           "America. They pay well per sale but move slowly — read “sells in” "
           "before committing a retainer slot.",
    "bulk": "Everything under 20,000 gil per unit: consumables, dyes, materia, "
            "aethersands. Thin per unit, but one slot holds a whole stack and "
            "these clear in hours rather than days.",
    "rev": "The other direction — buy anywhere on Materia, carry it home, sell on "
           "your NA world. Materia's stock is shallow, so check the stock column "
           "before planning a trip around any one line.",
}


def fmt(kind, v):
    if v is None:
        return "—"
    if kind == "gil":
        return f"{round(v):,}"
    if kind == "int":
        return f"{v:,}"
    if kind == "pct":
        return f"{v}%"
    if kind == "rate":
        return f"{v:,.0f}" if v >= 10 else f"{v:.2f}"
    if kind == "dur":
        if v < 0.042:
            return "<1h"
        if v < 1:
            return f"{round(v * 24)}h"
        if v < 60:
            return f"{v:.1f}d" if v < 10 else f"{round(v)}d"
        return "60d+"
    return str(v)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FFXIV Market Routes — Materia and NA")
        self.geometry("1320x800")
        self.minsize(960, 560)
        self.configure(bg=BG)

        self.results = None
        self.tier = "big"
        self.world = {}                 # tier -> chosen world
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="Loading…")
        self.sort_state = {t: ("t", True) for t in TIER_ORDER}
        self.trees = {}
        self.rows_on_screen = {}

        self.queue = queue.Queue()
        self.worker = None
        self.stop_flag = threading.Event()

        self._style()
        self._build()
        self.after(80, self._pump)
        self._load_cache()

    # -- chrome ------------------------------------------------------------

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f_body = (base, 10)
        self.f_bold = (base, 10, "bold")
        self.f_head = (base, 9, "bold")
        self.f_title = (base, 17, "bold")
        mono = "Consolas" if "Consolas" in tkfont.families() else "Courier New"

        s.configure(".", background=BG, foreground=INK, fieldbackground=PANEL_2,
                    bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=INK_2, font=self.f_body)
        s.configure("Head.TLabel", background=BG, foreground=INK, font=self.f_title)
        s.configure("Dim.TLabel", background=BG, foreground=INK_3, font=(base, 9))
        s.configure("Key.TLabel", background=PANEL, foreground=INK_3, font=self.f_head)
        s.configure("Val.TLabel", background=PANEL, foreground=INK, font=(mono, 15))
        s.configure("Note.TLabel", background=PANEL, foreground=INK_3, font=(base, 9))

        s.configure("TButton", background=PANEL_2, foreground=INK_2, font=self.f_body,
                    borderwidth=1, focusthickness=0, padding=(13, 6))
        s.map("TButton", background=[("active", LINE)], foreground=[("active", INK)])
        s.configure("Go.TButton", background=GOLD, foreground="#1a1406", font=self.f_bold)
        s.map("Go.TButton", background=[("active", "#e0b45e"), ("disabled", LINE)],
              foreground=[("disabled", INK_3)])
        s.configure("World.TButton", background=PANEL_2, foreground=INK_2, padding=(15, 6))
        s.map("World.TButton", background=[("active", LINE)])
        s.configure("WorldOn.TButton", background="#33291a", foreground=GOLD,
                    font=self.f_bold, padding=(15, 6))

        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        s.configure("TNotebook.Tab", background=BG, foreground=INK_3,
                    font=self.f_body, padding=(18, 8), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", BG)],
              foreground=[("selected", GOLD)])

        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=INK_2, rowheight=25, borderwidth=0, font=self.f_body)
        s.configure("Treeview.Heading", background=PANEL_2, foreground=INK_3,
                    font=self.f_head, relief="flat", padding=(6, 7))
        s.map("Treeview.Heading", background=[("active", LINE)],
              foreground=[("active", INK)])
        s.map("Treeview", background=[("selected", "#2c3446")],
              foreground=[("selected", INK)])
        s.configure("TEntry", fieldbackground=PANEL_2, foreground=INK,
                    insertcolor=INK, borderwidth=1)
        s.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2,
                    foreground=INK, arrowcolor=INK_3, borderwidth=1)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL_2)],
              foreground=[("readonly", INK)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", INK)
        self.option_add("*TCombobox*Listbox.selectBackground", "#33291a")
        self.option_add("*TCombobox*Listbox.selectForeground", GOLD)
        s.configure("Bar.Horizontal.TProgressbar", background=GOLD,
                    troughcolor=PANEL_2, borderwidth=0, thickness=4)
        s.configure("Alert.TFrame", background=ALERT_BG)
        s.configure("Alert.TLabel", background=ALERT_BG, foreground=WARN,
                    font=self.f_body)

    def _build(self):
        pad = {"padx": 18}

        top = ttk.Frame(self)
        top.pack(fill="x", pady=(16, 0), **pad)
        ttk.Label(top, text="Market routes", style="Head.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(top, style="Dim.TLabel")
        self.subtitle.pack(anchor="w", pady=(3, 0))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(14, 0), **pad)
        self.sell_label = ttk.Label(bar, text="SELLING ON", style="Dim.TLabel")
        self.sell_label.pack(side="left", padx=(0, 10))
        self.world_box = ttk.Frame(bar)
        self.world_box.pack(side="left")

        self.refresh_btn = ttk.Button(bar, text="Refresh data", style="Go.TButton",
                                      command=self.refresh)
        self.refresh_btn.pack(side="right")
        self.cancel_btn = ttk.Button(bar, text="Stop", command=self.cancel)
        ttk.Button(bar, text="Check for updates",
                   command=self.check_update).pack(side="right", padx=(0, 8))
        self.filter_entry = ttk.Entry(bar, textvariable=self.search, width=22,
                                      font=self.f_body)
        self.filter_entry.pack(side="right", padx=(0, 10))
        ttk.Label(bar, text="FILTER", style="Dim.TLabel").pack(side="right", padx=(0, 8))
        self.search.trace_add("write", lambda *_: self.fill())

        # Shown only when something is wrong or an update is waiting.
        self.banner = ttk.Frame(self, style="Alert.TFrame")
        self.banner_text = ttk.Label(self.banner, style="Alert.TLabel",
                                     wraplength=900, justify="left")
        self.banner_text.pack(side="left", padx=14, pady=10)
        self.banner_btn = ttk.Button(self.banner, text="Retry")
        self.banner_btn.pack(side="right", padx=(0, 12), pady=8)
        self.banner_dismiss = ttk.Button(self.banner, text="Dismiss",
                                         command=self.hide_banner)
        self.banner_dismiss.pack(side="right", padx=(0, 8), pady=8)

        self.strip = ttk.Frame(self, style="Panel.TFrame")
        self.strip.pack(fill="x", pady=(14, 0), **pad)

        self.progress = ttk.Progressbar(self, style="Bar.Horizontal.TProgressbar",
                                        mode="determinate", maximum=1000)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, pady=(12, 0), **pad)
        for tier in TIER_ORDER:
            frame = ttk.Frame(nb)
            nb.add(frame, text=engine.TIERS[tier]["label"])
            self.trees[tier] = self._table(frame, tier)
        nb.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.notebook = nb

        foot = ttk.Frame(self)
        foot.pack(fill="x", pady=(8, 4), **pad)
        ttk.Label(foot, textvariable=self.status, style="Dim.TLabel").pack(side="left")
        ttk.Label(foot, style="Dim.TLabel",
                  text="Ctrl+R refresh · Ctrl+F filter · [ ] sort · Enter reverse · "
                       "Space or double-click opens Universalis").pack(side="right")

        legal = ttk.Frame(self)
        legal.pack(fill="x", pady=(0, 12), **pad)
        ttk.Label(legal, style="Dim.TLabel", wraplength=1220, justify="left",
                  text="Estimates from crowd-sourced listings, not guarantees — prices "
                       "move and a row backed by a handful of sales is a guess. Market "
                       "data from Universalis; item names from XIVAPI. FINAL FANTASY XIV "
                       "© SQUARE ENIX CO., LTD. Unofficial tool, not affiliated with or "
                       "endorsed by Square Enix. Sends nothing anywhere and collects "
                       "nothing about you.").pack(anchor="w")

        self.bind_all("<Control-r>", lambda e: self.refresh())
        self.bind_all("<Control-f>", lambda e: self.focus_filter())
        self.bind_all("<Escape>", lambda e: self.cancel())

    def _table(self, parent, tier):
        blurb = ttk.Label(parent, style="Dim.TLabel", wraplength=1220, justify="left",
                          text=BLURB[tier])
        blurb.pack(anchor="w", pady=(4, 8))
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        cols = [c[0] for c in COLUMNS]
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for key, heading, width, anchor, _kind in COLUMNS:
            tree.heading(key, text=heading,
                         command=lambda k=key, t=tier: self.sort_by(t, k))
            tree.column(key, width=width,
                        anchor={"w": "w", "e": "e", "c": "center"}[anchor],
                        stretch=(key == "n"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("odd", background=PANEL_2)
        tree.tag_configure("free", foreground=GOOD)
        tree.bind("<Double-1>", lambda e, t=tier: self.open_item(t))
        # Sorting without a mouse: [ and ] walk the sort column, Enter reverses it.
        tree.bind("<bracketleft>", lambda e, t=tier: self.step_sort(t, -1))
        tree.bind("<bracketright>", lambda e, t=tier: self.step_sort(t, 1))
        tree.bind("<Return>", lambda e, t=tier: self.sort_by(t, self.sort_state[t][0]))
        tree.bind("<space>", lambda e, t=tier: self.open_item(t))
        return tree

    def _tab_changed(self, _event=None):
        idx = self.notebook.index(self.notebook.select())
        self.tier = TIER_ORDER[idx]
        self._world_picker()
        self._subtitle()
        self._strip()

    def _subtitle(self):
        if self.tier == "rev":
            text = ("Buy anywhere on Materia · sell on one NA world · "
                    "profits net of the 5% seller tax")
        else:
            text = ("Buy anywhere in North America · sell on one Materia world · "
                    "profits net of the 5% seller tax")
        self.subtitle.configure(text=text)

    # -- data --------------------------------------------------------------

    # -- banner ------------------------------------------------------------

    def show_banner(self, text, action=None, label="Retry"):
        self.banner_text.configure(text=text)
        if action:
            self.banner_btn.configure(text=label, command=action)
            self.banner_btn.pack(side="right", padx=(0, 12), pady=8)
        else:
            self.banner_btn.pack_forget()
        self.banner.pack(fill="x", padx=18, pady=(12, 0),
                         before=self.strip)

    def hide_banner(self):
        self.banner.pack_forget()

    def _probe(self):
        """Background health check so a dead API is obvious without a full scan."""
        def work():
            ok, msg = engine.health()
            self.queue.put(("health", (ok, msg), None))
        threading.Thread(target=work, daemon=True).start()

    def _load_cache(self):
        cached = engine.load_cached()
        if cached:
            self.apply(cached, note="from last scan")
        else:
            self.status.set("No data yet — hit Refresh to pull from Universalis "
                            "(takes a few minutes).")
            self._subtitle()
        self._probe()

    def refresh(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_flag.clear()
        self.refresh_btn.pack_forget()
        self.cancel_btn.pack(side="right")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))
        self.progress["value"] = 0
        self.status.set("Starting…")

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def work():
            try:
                self.queue.put(("done", engine.run_scan(report, self.stop_flag.is_set), None))
            except engine.Cancelled:
                self.queue.put(("cancelled", None, None))
            except engine.Unreachable as exc:
                self.queue.put(("unreachable", str(exc), None))
            except Exception as exc:                      # API shape, disk, etc.
                self.queue.put(("error", exc, None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.is_alive():
            self.stop_flag.set()
            self.status.set("Stopping…")

    def focus_filter(self):
        self.filter_entry.focus_set()
        self.filter_entry.select_range(0, "end")

    def _pump(self):
        try:
            while True:
                kind, payload, frac = self.queue.get_nowait()
                if kind == "progress":
                    self.status.set(payload)
                    if frac is not None:
                        self.progress["value"] = frac * 1000
                elif kind == "done":
                    self._end_scan()
                    self.apply(payload, note="just now")
                elif kind == "cancelled":
                    self._end_scan()
                    self.status.set("Scan stopped. Showing the previous data.")
                elif kind == "unreachable":
                    self._end_scan()
                    self.status.set("Couldn't reach Universalis.")
                    stale = (f" Still showing the scan from {self.results['stamp']}."
                             if self.results else " There's no earlier data to fall back on.")
                    self.show_banner(f"{payload}{stale}", self.refresh, "Try again")
                elif kind == "error":
                    self._end_scan()
                    self.status.set("Scan failed.")
                    self.show_banner(
                        f"The scan stopped with an unexpected error: {payload}",
                        self.refresh, "Try again")
                elif kind == "health":
                    ok, msg = payload
                    if not ok:
                        self.show_banner(
                            msg + (" Showing the last scan instead."
                                   if self.results else ""),
                            self._probe, "Check again")
                    else:
                        self.hide_banner()
                elif kind == "update":
                    self._update_result(payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _end_scan(self):
        self.progress.pack_forget()
        self.cancel_btn.pack_forget()
        self.refresh_btn.pack(side="right")

    def apply(self, results, note=""):
        self.results = results
        for tier in TIER_ORDER:
            options = self.world_options(tier)
            if options and self.world.get(tier) not in options:
                self.world[tier] = options[0]
        self._world_picker()
        self._subtitle()
        self.fill()
        fails = results.get("fails", 0)
        extra = f" · {fails} requests failed" if fails else ""
        self.status.set(f"Data {note} — scanned {results['stamp']}{extra}")

    def world_options(self, tier):
        """Flat, DC-ordered list of worlds you could be selling on for this tab."""
        menu = (self.results or {}).get("menus", {}).get(tier, {})
        return [w for dc in sorted(menu) for w in menu[dc]]

    def _world_picker(self):
        """Buttons for a handful of worlds, a dropdown for thirty-two."""
        for child in self.world_box.winfo_children():
            child.destroy()
        if not self.results:
            return
        menu = self.results["menus"][self.tier]
        options = self.world_options(self.tier)
        if not options:
            return
        current = self.world.get(self.tier, options[0])

        if len(options) <= 8:
            for w in options:
                ttk.Button(self.world_box, text=w,
                           style="WorldOn.TButton" if w == current else "World.TButton",
                           command=lambda x=w: self.pick(x)).pack(side="left", padx=(0, 6))
        else:
            # Data center first, then the worlds inside it.
            dc_of = {w: dc for dc, ws in menu.items() for w in ws}
            self.dc_var = tk.StringVar(value=dc_of.get(current, sorted(menu)[0]))
            self.w_var = tk.StringVar(value=current)
            dc_box = ttk.Combobox(self.world_box, textvariable=self.dc_var, width=11,
                                  state="readonly", values=sorted(menu), font=self.f_body)
            dc_box.pack(side="left", padx=(0, 6))
            w_box = ttk.Combobox(self.world_box, textvariable=self.w_var, width=15,
                                 state="readonly", values=menu[self.dc_var.get()],
                                 font=self.f_body)
            w_box.pack(side="left")

            def on_dc(_e=None):
                w_box.configure(values=menu[self.dc_var.get()])
                self.pick(menu[self.dc_var.get()][0])
                self.w_var.set(self.world[self.tier])

            dc_box.bind("<<ComboboxSelected>>", on_dc)
            w_box.bind("<<ComboboxSelected>>", lambda e: self.pick(self.w_var.get()))

    def pick(self, world):
        self.world[self.tier] = world
        if len(self.world_options(self.tier)) <= 8:
            self._world_picker()
        self.fill()

    def rows_for(self, tier):
        if not self.results:
            return []
        world = self.world.get(tier)
        needle = self.search.get().strip().lower()
        out = []
        for r in self.results["tables"].get(tier, []):
            v = r["w"].get(world)
            if not v:
                continue
            if needle and needle not in r["n"].lower() and needle not in r["c"].lower():
                continue
            row = dict(v)
            row.update({"id": r["id"], "n": r["n"], "c": r["c"], "q": r["q"],
                        "st": r["st"], "b": r["b"], "nau": r["nau"],
                        "stop": f"{r['stop']} ×{r['su']:,}"})
            out.append(row)
        key, desc = self.sort_state[tier]
        out.sort(key=lambda r: (r[key] is None, r[key]), reverse=desc)
        return out

    def sort_by(self, tier, key):
        cur, desc = self.sort_state[tier]
        self.sort_state[tier] = (key, not desc if key == cur else key != "n")
        self.fill()

    def step_sort(self, tier, delta):
        keys = [c[0] for c in COLUMNS]
        i = (keys.index(self.sort_state[tier][0]) + delta) % len(keys)
        self.sort_state[tier] = (keys[i], keys[i] != "n")
        self.fill()

    def fill(self):
        if not self.results:
            return
        for tier in TIER_ORDER:
            tree = self.trees[tier]
            tree.delete(*tree.get_children())
            rows = self.rows_for(tier)
            self.rows_on_screen[tier] = rows
            for i, r in enumerate(rows):
                values = [fmt(kind, r.get(key)) for key, _h, _w, _a, kind in COLUMNS]
                tags = ["odd"] if i % 2 else []
                if r.get("a") == 0:
                    tags.append("free")
                tree.insert("", "end", iid=str(i), values=values, tags=tags)
            key, desc = self.sort_state[tier]
            for k, heading, _w, _a, _kind in COLUMNS:
                arrow = "  ▼" if desc else "  ▲"
                tree.heading(k, text=heading + (arrow if k == key else ""))
        self._strip()

    def _strip(self):
        for child in self.strip.winfo_children():
            child.destroy()
        rows = self.rows_for(self.tier)
        world = self.world.get(self.tier, "—")
        if not rows:
            ttk.Label(self.strip, style="Note.TLabel",
                      text="  Nothing tradeable on this world with the current filter."
                      ).pack(anchor="w", padx=14, pady=14)
            return
        top = max(rows, key=lambda r: r["t"])
        fattest = max(rows, key=lambda r: r["p"])
        clear = sum(1 for r in rows if r["a"] == 0)
        cells = [
            ("BEST RUN HERE", f"{top['t']:,}",
             f"{top['n']} · buy {top['u']:,} · {fmt('dur', top['d'])} per sale"),
            ("FATTEST MARGIN", f"{fattest['p']:,}",
             f"{fattest['n']} · {fattest['m']}% per unit"),
            ("UNCONTESTED LINES", f"{clear} / {len(rows)}",
             "nothing listed under you"),
            ("SELLING ON", world, f"{len(rows):,} routes on this board"),
        ]
        for i, (k, v, n) in enumerate(cells):
            cell = ttk.Frame(self.strip, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            self.strip.columnconfigure(i, weight=1, uniform="strip")
            ttk.Label(cell, text=k, style="Key.TLabel").pack(anchor="w", padx=14,
                                                             pady=(12, 2))
            ttk.Label(cell, text=v, style="Val.TLabel").pack(anchor="w", padx=14)
            ttk.Label(cell, text=n, style="Note.TLabel", wraplength=260).pack(
                anchor="w", padx=14, pady=(2, 12))

    # -- updates -----------------------------------------------------------

    def check_update(self):
        self.status.set("Checking GitHub for an update…")

        def work():
            try:
                self.queue.put(("update", ("ok", update.check()), None))
            except update.UpdateError as exc:
                self.queue.put(("update", ("fail", str(exc)), None))

        threading.Thread(target=work, daemon=True).start()

    def _update_result(self, payload):
        kind, body = payload
        if kind == "fail":
            self.status.set("Update check failed.")
            self.show_banner(body, self.check_update, "Try again")
            return
        if not body["available"]:
            if not body["known"]:
                update.remember(body["sha"], body["slug"])
            self.status.set(f"Up to date with {body['slug']} ({body['short']}).")
            self.hide_banner()
            return
        note = f" — {body['message']}" if body["message"] else ""
        self.status.set(f"Update {body['short']} available.")
        self.show_banner(
            f"An update is available from {body['slug']}: {body['short']}"
            f" ({body['date']}){note}",
            lambda: self._do_update(body), "Install")

    def _do_update(self, info):
        if not messagebox.askyesno(
                "Install update",
                f"Replace this app's files with {info['short']} from "
                f"{info['slug']}?\n\nThe current version is copied to "
                f".cache/backup first. You'll need to restart the app afterwards."):
            return
        try:
            changed = update.install(info)
        except update.UpdateError as exc:
            self.status.set("Update failed.")
            self.show_banner(str(exc), self.check_update, "Try again")
            return
        self.hide_banner()
        self.status.set("Updated — restart to run the new version.")
        messagebox.showinfo(
            "Update installed",
            "Replaced: " + ", ".join(changed) +
            "\n\nClose and reopen the app to run it.")

    def open_item(self, tier):
        sel = self.trees[tier].selection()
        if not sel:
            return
        rows = self.rows_on_screen.get(tier, [])
        idx = int(sel[0])
        if idx < len(rows):
            webbrowser.open(f"https://universalis.app/market/{rows[idx]['id']}")


if __name__ == "__main__":
    App().mainloop()
