#!/usr/bin/env python3
"""
Desktop window for the FFXIV Materia <-> NA arbitrage scanner.

Each tab sells in a different place, so each tab has its own world picker: the
NA -> Materia tabs pick one of the five Materia worlds, and the Materia -> NA tab
picks one of the thirty-two NA worlds. Market boards are per-world, so the world
you choose is what the sell price, the competition and the sale rate are measured
against.

Click the tick column to put a row on your shopping list, which groups the run by
which world to visit and totals it against your gil and retainer slots.

Everything runs locally against the Universalis API; no account or key needed.

Run:  python app.py      (or double-click run.bat on Windows)
"""

import io
import os
import queue
import sys
import threading
import time
from datetime import datetime
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import engine
import paths
import store
import update

# ----------------------------------------------------------------------------
# Look
# ----------------------------------------------------------------------------

# Colours come from the saved theme (Settings tab). These module-level names
# are rebound on load and on every change, so everything that reads them picks
# up the new palette when the styles are rebuilt.
BG = PANEL = PANEL_2 = LINE = INK = INK_2 = INK_3 = GOLD = GOOD = WARN = ""
ALERT_BG = ""


def _mix(a, b, t):
    """Blend two #rrggbb colours -- used to derive tints from the palette."""
    try:
        ar, ag, ab = (int(a[i:i + 2], 16) for i in (1, 3, 5))
        br, bg_, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return a
    return "#%02x%02x%02x" % (round(ar + (br - ar) * t),
                              round(ag + (bg_ - ag) * t),
                              round(ab + (bb - ab) * t))


def apply_theme(theme):
    """Rebind the palette from a saved theme."""
    global BG, PANEL, PANEL_2, LINE, INK, INK_2, INK_3, GOLD, GOOD, WARN, ALERT_BG
    BG = theme["ground"]
    PANEL = theme["surface"]
    PANEL_2 = theme["surface2"]
    LINE = theme["line"]
    INK = theme["ink"]
    INK_2 = theme["ink2"]
    INK_3 = theme["ink3"]
    GOLD = theme["accent"]
    GOOD = theme["good"]
    WARN = theme["warn"]
    # The alert strip is a wash of the warning colour over the panel, so it
    # stays legible whichever palette is in use rather than being a fixed brown.
    ALERT_BG = _mix(PANEL, WARN, 0.18)


apply_theme(store.load_theme())

DPI_MODE = "unknown"   # set by enable_hidpi(), shown in About

TIER_ORDER = ["big", "bulk", "rev"]
TAB_NAMES = ["Big ticket", "Bulk & consumables", "Materia → NA",
             "My listings", "Look up an item", "Shopping list"]

# key, heading, width, anchor, kind
COLUMNS = [
    ("pick", "",                  28, "c", "pick"),
    ("n",    "Item",             230, "w", "text"),
    ("q",    "Q",                 34, "c", "text"),
    ("b",    "Buy",               86, "e", "gil"),
    ("stop", "Best stop",        132, "w", "stop"),
    ("s",    "Sell price",        88, "e", "gil"),
    ("p",    "Net / unit",        88, "e", "gil"),
    ("dp",   "Change",            78, "e", "delta"),
    ("m",    "Margin",            62, "e", "pct"),
    ("a",    "Ahead",             54, "e", "int"),
    ("r",    "Sells / day",       78, "e", "rate"),
    ("d",    "Sells in",          70, "e", "dur"),
    ("t",    "Trip value",        96, "e", "gil"),
    ("u",    "Buy qty",           66, "e", "int"),
    ("g",    "Gil / slot / day",  98, "e", "gil"),
    ("nau",  "Stock",             62, "e", "int"),
]

# Column widths for the tables that aren't built from COLUMNS, so a monitor
# change can re-apply them at the new scale.
BASKET_WIDTHS = (("what", 300), ("q", 34), ("qty", 60), ("buy", 90),
                 ("spend", 110), ("sell", 90), ("profit", 90), ("gain", 110),
                 ("world", 120))
POSITION_WIDTHS = (("name", 250), ("q", 34), ("world", 110), ("price", 100),
                   ("qty", 60), ("low", 100), ("under", 80), ("state", 200),
                   ("when", 90))
LOOKUP_WIDTHS = (("region", 120), ("world", 110), ("q", 34), ("buy", 105),
                 ("units", 75), ("sell", 100), ("rate", 85), ("sold", 60),
                 ("net", 95), ("margin", 70))

BLURB = {
    "big": "Items netting 20,000 gil or more per unit, bought anywhere in North "
           "America. They pay well per sale but move slowly — read “sells in” "
           "before committing a retainer slot.",
    "bulk": "Everything under 20,000 gil per unit: consumables, dyes, materia, "
            "aethersands. Thin per unit, but one slot holds a whole stack and "
            "these clear in hours rather than days.",
    "rev": "The other direction — buy anywhere on Materia, carry it home, sell on "
           "your NA world. Materia's stock is shallow, so check “buy qty” before "
           "planning a trip around any one line.",
}


def fmt(kind, v):
    if kind == "pick":
        return "✓" if v else "·"
    if v is None:
        return "—"
    if kind == "gil":
        return f"{round(v):,}"
    if kind == "int":
        return f"{v:,}"
    if kind == "pct":
        return f"{v}%"
    if kind == "delta":
        if v == 0:
            return "—"
        return f"{'▲' if v > 0 else '▼'} {abs(round(v)):,}"
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


def enable_hidpi():
    """Declare how this process handles displays of differing DPI.

    Three levels, best first, because which is available depends on the Windows
    build:

      Per-monitor v2  what we want. Windows reports the DPI of whichever
                      monitor a window is on, scales non-client areas like the
                      title bar for us, and tells the window when it moves
                      between displays.
      Per-monitor v1  the older flag. Stops Windows bitmap-scaling us, but
                      GetDpiForWindow can keep answering with the DPI the
                      process started on -- which shows up as the window
                      staying visually "zoomed in" after being dragged to a
                      lower-scale monitor, because nothing ever tells us the
                      scale changed.
      System aware    last resort: crisp on the primary display, bitmap-scaled
                      (soft, but correctly sized) everywhere else.

    Returns the scale of the display the process starts on.
    """
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        level = None
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                level = "per-monitor v2"
        except (AttributeError, OSError):
            pass
        if level is None:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
                level = "per-monitor v1"
            except (AttributeError, OSError):
                try:
                    user32.SetProcessDPIAware()
                    level = "system aware"
                except (AttributeError, OSError):
                    level = "none"
        globals()["DPI_MODE"] = level

        dc = user32.GetDC(0)
        try:
            scale = ctypes.windll.gdi32.GetDeviceCaps(dc, 88) / 96.0  # LOGPIXELSX
        finally:
            user32.ReleaseDC(0, dc)
    except Exception:
        globals()["DPI_MODE"] = "unknown"
        return 1.0
    return scale if 0.5 <= scale <= 4.0 else 1.0


def window_scale(widget, fallback=1.0):
    """DPI scale of the monitor this window is currently on.

    enable_hidpi() declares the process per-monitor DPI aware, which stops
    Windows bitmap-scaling us -- crisp on a 4K display, but it also means
    nothing is rescaled for us when the window is dragged to a monitor at a
    different scale. So we have to ask, per window, and redo the sizing.
    """
    if sys.platform != "win32":
        return fallback
    try:
        import ctypes
        hwnd = int(widget.winfo_id())
        # GetDpiForWindow is Windows 10 1607+ and returns the DPI of the
        # monitor the window is on, which is exactly the question.
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            scale = dpi / 96.0
            return scale if 0.5 <= scale <= 4.0 else fallback
    except Exception:
        pass
    return fallback


def age_text(hours):
    if hours is None:
        return "no data yet"
    if hours < 1:
        return f"{round(hours * 60)} min old"
    if hours < 48:
        return f"{round(hours)} h old"
    return f"{round(hours / 24)} days old"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.scale = enable_hidpi()
        self.title(f"FFXIV Market Routes {paths.VERSION} — Materia and NA")
        # Size the window to the display, not to a fixed pixel count: 1420px is
        # a sensible window on a 1080p screen and a postage stamp on a 4K one.
        width = min(int(1420 * self.scale), self.winfo_screenwidth() - 80)
        height = min(int(830 * self.scale), self.winfo_screenheight() - 120)
        self.geometry(f"{width}x{height}")
        self.minsize(int(980 * min(self.scale, 1.5)),
                     int(580 * min(self.scale, 1.5)))
        self.configure(bg=BG)
        # Tk's own scaling governs anything measured in points.
        self.tk.call("tk", "scaling", self.scale * 1.3333)
        # Replace Tk's default feather. `default=` applies it to every window
        # the app opens, so the detail and settings windows match too.
        try:
            self.iconbitmap(default=paths.resource("icon.ico"))
        except (tk.TclError, OSError):
            pass

        self.settings = store.load_settings()
        self.basket = store.load_basket()
        self.positions = store.load_positions()
        if store.migrate_positions(self.positions):
            store.save_positions(self.positions)
        self.name_index = engine.load_name_index()
        self.theme = store.load_theme()
        self.swatches = {}
        self.found = []
        self.lookup_result = None
        self.position_keys = []
        self.results = None
        self.tier = "big"
        self.world = dict(self.settings.get("worlds") or {})
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="Loading…")
        self.sort_state = {t: ("t", True) for t in TIER_ORDER}
        self.trees = {}
        self.rows_on_screen = {}

        self.queue = queue.Queue()
        self.worker = None
        self.job = None                 # "scan" or "positions", for error recovery
        self.stop_flag = threading.Event()

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # Dragging between monitors of different scale needs a re-layout; Tk
        # will not do it for us once we are per-monitor DPI aware.
        self._rescale_job = None
        self.bind("<Configure>", self._maybe_rescale)
        self.after(80, self._pump)
        self._load_cache()
        self.after(2000, self._auto_tick)

    def _maybe_rescale(self, event=None):
        """Debounced: a drag fires <Configure> continuously."""
        if event is not None and event.widget is not self:
            return
        if self._rescale_job is not None:
            self.after_cancel(self._rescale_job)
        self._rescale_job = self.after(220, self._apply_monitor_scale)

    def _apply_monitor_scale(self):
        self._rescale_job = None
        found = window_scale(self, self.scale)
        # A tenth of a step is the smallest change worth a full re-layout;
        # below that it is just noise from rounding.
        if abs(found - self.scale) < 0.05:
            return
        old, self.scale = self.scale, found
        self.tk.call("tk", "scaling", self.scale * 1.3333)
        self._style()                       # row heights, fonts, the palette
        self._resize_columns()
        self._fit_to_screen()
        self.status.set(f"Display scale changed ({old:.2f} to {found:.2f}) — "
                        f"resized to match this monitor.")

    def _fit_to_screen(self):
        """Shrink the window if it is now wider or taller than the display.

        A window sized for a 4K monitor is bigger than a 1080p screen, so after
        moving there its edges can sit off the display with no way to grab them.
        """
        try:
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            width = min(self.winfo_width(), screen_w - self.px(40))
            height = min(self.winfo_height(), screen_h - self.px(80))
            if width < self.winfo_width() or height < self.winfo_height():
                x = max(0, min(self.winfo_x(), screen_w - width))
                y = max(0, min(self.winfo_y(), screen_h - height))
                self.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
        except tk.TclError:
            pass

    def _resize_columns(self):
        """Column widths are raw pixels, so they need redoing by hand."""
        for tier, tree in self.trees.items():
            for key, _label, width, _anchor, _kind in COLUMNS:
                try:
                    tree.column(key, width=self.px(width))
                except tk.TclError:
                    pass
        for tree, widths in ((getattr(self, "basket_tree", None), BASKET_WIDTHS),
                             (getattr(self, "positions_tree", None), POSITION_WIDTHS),
                             (getattr(self, "find_tree", None), LOOKUP_WIDTHS)):
            if tree is None:
                continue
            for key, width in widths:
                try:
                    tree.column(key, width=self.px(width))
                except tk.TclError:
                    pass

    # -- chrome ------------------------------------------------------------

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]

        # Tk point sizes already follow tk scaling, so fonts don't need scaling
        # again -- but row heights and column widths are raw pixels and do.
        def px(n):
            return int(round(n * self.scale))
        self.px = px

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
        s.configure("ValWarn.TLabel", background=PANEL, foreground=WARN, font=(mono, 15))
        s.configure("Note.TLabel", background=PANEL, foreground=INK_3, font=(base, 9))

        s.configure("TButton", background=PANEL_2, foreground=INK_2, font=self.f_body,
                    borderwidth=1, focusthickness=0, padding=(13, 6))
        s.map("TButton", background=[("active", LINE)], foreground=[("active", INK)])
        s.configure("Go.TButton", background=GOLD, foreground=_mix(GOLD, BG, 0.82),
                    font=self.f_bold)
        s.map("Go.TButton", background=[("active", _mix(GOLD, INK, 0.25)),
                                        ("disabled", LINE)],
              foreground=[("disabled", INK_3)])
        s.configure("World.TButton", background=PANEL_2, foreground=INK_2, padding=(15, 6))
        s.map("World.TButton", background=[("active", LINE)])
        s.configure("WorldOn.TButton", background=_mix(PANEL, GOLD, 0.2), foreground=GOLD,
                    font=self.f_bold, padding=(15, 6))
        # The auto-refresh toggle reads as on or off at a glance.
        s.configure("Off.TButton", background=PANEL_2, foreground=INK_3, padding=(12, 6))
        s.map("Off.TButton", background=[("active", LINE)])
        s.configure("On.TButton", background=_mix(PANEL, GOOD, 0.2), foreground=GOOD,
                    font=self.f_bold, padding=(12, 6))
        s.map("On.TButton", background=[("active", _mix(PANEL, GOOD, 0.32))])

        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        s.configure("TNotebook.Tab", background=BG, foreground=INK_3,
                    font=self.f_body, padding=(18, 8), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", BG)],
              foreground=[("selected", GOLD)])

        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=INK_2, rowheight=px(25), borderwidth=0,
                    font=self.f_body)
        s.configure("Treeview.Heading", background=PANEL_2, foreground=INK_3,
                    font=self.f_head, relief="flat", padding=(6, 7))
        s.map("Treeview.Heading", background=[("active", LINE)],
              foreground=[("active", INK)])
        s.map("Treeview", background=[("selected", _mix(PANEL_2, INK, 0.12))],
              foreground=[("selected", INK)])
        s.configure("TEntry", fieldbackground=PANEL_2, foreground=INK,
                    insertcolor=INK, borderwidth=1)
        s.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2,
                    foreground=INK, arrowcolor=INK_3, borderwidth=1)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL_2)],
              foreground=[("readonly", INK)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", INK)
        self.option_add("*TCombobox*Listbox.selectBackground", _mix(PANEL, GOLD, 0.2))
        self.option_add("*TCombobox*Listbox.selectForeground", GOLD)
        s.configure("Bar.Horizontal.TProgressbar", background=GOLD,
                    troughcolor=PANEL_2, borderwidth=0, thickness=4)
        s.configure("Chk.TCheckbutton", background=BG, foreground=INK_2,
                    font=self.f_body, focuscolor=BG)
        s.map("Chk.TCheckbutton", background=[("active", BG)],
              foreground=[("active", INK)],
              indicatorcolor=[("selected", GOLD), ("!selected", PANEL_2)])
        s.configure("Alert.TFrame", background=ALERT_BG)
        s.configure("Alert.TLabel", background=ALERT_BG, foreground=WARN,
                    font=self.f_body)

    def _menubar(self):
        bar = tk.Menu(self)
        opts = {"background": PANEL, "foreground": INK, "activebackground": "#33291a",
                "activeforeground": GOLD, "borderwidth": 0}

        data = tk.Menu(bar, tearoff=0, **opts)
        data.add_command(label="Refresh now\tCtrl+R", command=self.refresh)
        data.add_command(label="Stop scan\tEsc", command=self.cancel)
        data.add_separator()
        self.open_var = tk.BooleanVar(value=self.settings["refresh_on_open"])
        data.add_checkbutton(label="Refresh when the app opens, if data is stale",
                             variable=self.open_var, command=self._save_prefs,
                             background=PANEL, foreground=INK,
                             activebackground="#33291a", activeforeground=GOLD,
                             selectcolor=GOLD)
        self.upd_var = tk.BooleanVar(
            value=self.settings.get("check_updates_on_open", True))
        data.add_checkbutton(label="Check for a new version when the app opens",
                             variable=self.upd_var, command=self._save_prefs,
                             background=PANEL, foreground=INK,
                             activebackground="#33291a", activeforeground=GOLD,
                             selectcolor=GOLD)
        bar.add_cascade(label="Data", menu=data)

        setmenu = tk.Menu(bar, tearoff=0, **opts)
        setmenu.add_command(label="Colours…", command=self.open_settings)
        setmenu.add_command(label="My retainers…", command=self.open_settings)
        setmenu.add_separator()
        setmenu.add_command(label="Open the data folder",
                            command=self.open_data_folder)
        bar.add_cascade(label="Settings", menu=setmenu)

        helpm = tk.Menu(bar, tearoff=0, **opts)
        helpm.add_command(label="Check for updates", command=self.check_update)
        helpm.add_command(label="Open Universalis",
                          command=lambda: webbrowser.open("https://universalis.app/"))
        helpm.add_command(label="About", command=self.about)
        bar.add_cascade(label="Help", menu=helpm)
        self.configure(menu=bar)

    def _save_prefs(self):
        self.settings["refresh_on_open"] = self.open_var.get()
        self.settings["check_updates_on_open"] = self.upd_var.get()
        store.save_settings(self.settings)

    def about(self):
        messagebox.showinfo(
            "About",
            f"FFXIV Market Routes {paths.VERSION}\n"
            f"{'built as an .exe' if paths.FROZEN else 'running from source'}"
            f" · Python {sys.version.split()[0]}\n"
            f"Display: {self.scale:.2f}x scaling, {DPI_MODE}\n"
            f"Data folder: {paths.DATA_DIR}\n\n"
            "Finds items worth buying on one side of the Materia / North America "
            "divide and selling on the other.\n\n"
            "Market data from Universalis, item names from XIVAPI. Estimates from "
            "crowd-sourced listings, not guarantees.\n\n"
            "Unofficial tool, not affiliated with or endorsed by Square Enix.\n"
            "FINAL FANTASY XIV © SQUARE ENIX CO., LTD.")

    def _build(self):
        pad = {"padx": 18}
        self._menubar()

        top = ttk.Frame(self)
        top.pack(fill="x", pady=(14, 0), **pad)
        ttk.Label(top, text="Market routes", style="Head.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(top, style="Dim.TLabel")
        self.subtitle.pack(anchor="w", pady=(3, 0))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(12, 0), **pad)
        self.sell_label = ttk.Label(bar, text="SELLING ON", style="Dim.TLabel")
        self.sell_label.pack(side="left", padx=(0, 10))
        self.world_box = ttk.Frame(bar)
        self.world_box.pack(side="left")

        self.refresh_btn = ttk.Button(bar, text="Refresh data", style="Go.TButton",
                                      command=self.refresh)
        self.refresh_btn.pack(side="right")
        self.cancel_btn = ttk.Button(bar, text="Stop", command=self.cancel)
        self.row_btn = ttk.Button(bar, text="Refresh this item",
                                  command=self.refresh_row)
        self.row_btn.pack(side="right", padx=(0, 8))
        self.auto_btn = ttk.Button(bar, command=self.toggle_auto)
        self.auto_btn.pack(side="right", padx=(0, 8))
        self._paint_auto()
        self.filter_entry = ttk.Entry(bar, textvariable=self.search, width=20,
                                      font=self.f_body)
        self.filter_entry.pack(side="right", padx=(0, 10))
        ttk.Label(bar, text="FILTER", style="Dim.TLabel").pack(side="right", padx=(0, 8))
        self.search.trace_add("write", lambda *_: self.fill())

        filt = ttk.Frame(self)
        filt.pack(fill="x", pady=(8, 0), **pad)
        self.afford_var = tk.BooleanVar(value=self.settings["affordable_only"])
        self.uncon_var = tk.BooleanVar(value=self.settings["uncontested_only"])
        self.margin_var = tk.StringVar(value=str(self.settings["min_margin"] or ""))
        ttk.Checkbutton(filt, text="Only what I can afford", variable=self.afford_var,
                        style="Chk.TCheckbutton",
                        command=self._filters_changed).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(filt, text="Uncontested only", variable=self.uncon_var,
                        style="Chk.TCheckbutton",
                        command=self._filters_changed).pack(side="left", padx=(0, 16))
        ttk.Label(filt, text="MIN MARGIN %", style="Dim.TLabel").pack(side="left",
                                                                     padx=(0, 6))
        ttk.Entry(filt, textvariable=self.margin_var, width=6,
                  font=self.f_body).pack(side="left")
        self.margin_var.trace_add("write", lambda *_: self._filters_changed())
        self.filter_note = ttk.Label(filt, style="Dim.TLabel")
        self.filter_note.pack(side="left", padx=(16, 0))

        self.banner = ttk.Frame(self, style="Alert.TFrame")
        self.banner_text = ttk.Label(self.banner, style="Alert.TLabel",
                                     wraplength=880, justify="left")
        self.banner_text.pack(side="left", padx=14, pady=10)
        self.banner_btn = ttk.Button(self.banner, text="Retry")
        self.banner_btn.pack(side="right", padx=(0, 12), pady=8)
        ttk.Button(self.banner, text="Dismiss", command=self.hide_banner).pack(
            side="right", padx=(0, 8), pady=8)

        self.strip = ttk.Frame(self, style="Panel.TFrame")
        self.strip.pack(fill="x", pady=(12, 0), **pad)

        self.progress = ttk.Progressbar(self, style="Bar.Horizontal.TProgressbar",
                                        mode="determinate", maximum=1000)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, pady=(10, 0), **pad)
        for tier in TIER_ORDER:
            frame = ttk.Frame(nb)
            nb.add(frame, text=engine.TIERS[tier]["label"])
            self.trees[tier] = self._table(frame, tier)
        pos_frame = ttk.Frame(nb)
        nb.add(pos_frame, text="My listings")
        self._positions_tab(pos_frame)
        find_frame = ttk.Frame(nb)
        nb.add(find_frame, text="Look up an item")
        self._search_tab(find_frame)
        basket_frame = ttk.Frame(nb)
        nb.add(basket_frame, text="Shopping list")
        self._basket_tab(basket_frame)
        self.after(120, self.draw_positions)
        nb.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.notebook = nb

        foot = ttk.Frame(self)
        foot.pack(fill="x", pady=(8, 4), **pad)
        ttk.Label(foot, textvariable=self.status, style="Dim.TLabel").pack(side="left")
        ttk.Label(foot, style="Dim.TLabel",
                  text="Click ✓ to add · Ctrl+R full scan · F5 refresh one item · "
                       "Ctrl+F filter · [ ] sort · Space opens Universalis").pack(side="right")

        legal = ttk.Frame(self)
        legal.pack(fill="x", pady=(0, 12), **pad)
        ttk.Label(legal, style="Dim.TLabel", wraplength=1320, justify="left",
                  text="Estimates from crowd-sourced listings, not guarantees — prices "
                       "move and a row backed by a handful of sales is a guess. Market "
                       "data from Universalis; item names from XIVAPI. FINAL FANTASY XIV "
                       "© SQUARE ENIX CO., LTD. Unofficial tool, not affiliated with or "
                       "endorsed by Square Enix. Sends nothing anywhere and collects "
                       "nothing about you.").pack(anchor="w")

        self.bind_all("<Control-r>", lambda e: self.refresh())
        self.bind_all("<F5>", lambda e: self.refresh_row())
        self.bind_all("<Control-f>", lambda e: self.focus_filter())
        self.bind_all("<Escape>", lambda e: self.cancel())

    def _table(self, parent, tier):
        ttk.Label(parent, style="Dim.TLabel", wraplength=1320, justify="left",
                  text=BLURB[tier]).pack(anchor="w", pady=(4, 8))
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        cols = [c[0] for c in COLUMNS]
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for key, heading, width, anchor, _kind in COLUMNS:
            tree.heading(key, text=heading,
                         command=lambda k=key, t=tier: self.sort_by(t, k))
            tree.column(key, width=self.px(width),
                        anchor={"w": "w", "e": "e", "c": "center"}[anchor],
                        stretch=(key == "n"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("odd", background=PANEL_2)
        tree.tag_configure("free", foreground=GOOD)
        tree.tag_configure("picked", foreground=GOLD)
        tree.bind("<Button-1>", lambda e, t=tier: self._table_click(e, t))
        tree.bind("<Double-1>", lambda e, t=tier: self.show_detail(t))
        tree.bind("<space>", lambda e, t=tier: self.open_item(t))
        tree.bind("<plus>", lambda e, t=tier: self.toggle_selected(t))
        tree.bind("<bracketleft>", lambda e, t=tier: self.step_sort(t, -1))
        tree.bind("<bracketright>", lambda e, t=tier: self.step_sort(t, 1))
        tree.bind("<Return>", lambda e, t=tier: self.sort_by(t, self.sort_state[t][0]))
        return tree

    def _basket_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(6, 10))
        ttk.Label(top, text="GIL ON HAND", style="Dim.TLabel").pack(side="left",
                                                                   padx=(0, 8))
        self.gil_var = tk.StringVar(value=str(self.settings["budget_gil"] or ""))
        ttk.Entry(top, textvariable=self.gil_var, width=14,
                  font=self.f_body).pack(side="left", padx=(0, 18))
        ttk.Label(top, text="RETAINER SLOTS", style="Dim.TLabel").pack(side="left",
                                                                      padx=(0, 8))
        self.slot_var = tk.StringVar(value=str(self.settings["retainer_slots"]))
        ttk.Entry(top, textvariable=self.slot_var, width=6,
                  font=self.f_body).pack(side="left")
        self.gil_var.trace_add("write", lambda *_: self._budget_changed())
        self.slot_var.trace_add("write", lambda *_: self._budget_changed())

        ttk.Button(top, text="Clear list", command=self.clear_basket).pack(side="right")
        ttk.Button(top, text="Remove selected", command=self.remove_selected).pack(
            side="right", padx=(0, 8))
        ttk.Button(top, text="Copy as text", command=self.copy_basket).pack(
            side="right", padx=(0, 8))

        self.basket_strip = ttk.Frame(parent, style="Panel.TFrame")
        self.basket_strip.pack(fill="x", pady=(0, 10))

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        heads = [("what", "Item / buy on", 300, "w"), ("q", "Q", 34, "center"),
                 ("qty", "Qty", 60, "e"), ("buy", "Unit cost", 90, "e"),
                 ("spend", "Spend", 110, "e"), ("sell", "Unit price", 90, "e"),
                 ("profit", "Net / unit", 90, "e"), ("gain", "Return", 110, "e"),
                 ("world", "Sell on", 120, "w")]
        tree = ttk.Treeview(wrap, columns=[h[0] for h in heads],
                            show="tree headings", selectmode="extended")
        tree.column("#0", width=0, stretch=False)
        for key, label, width, anchor in heads:
            tree.heading(key, text=label)
            tree.column(key, width=self.px(width), anchor=anchor,
                        stretch=(key == "what"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("group", foreground=GOLD, background=PANEL_2)
        tree.tag_configure("odd", background=PANEL_2)
        self.basket_tree = tree

    # -- refreshing one row ------------------------------------------------

    def refresh_row(self, tier=None, item=None):
        """Re-price a single item. Five requests instead of a five-minute scan."""
        if self.worker and self.worker.is_alive():
            self.status.set("Already busy — let the current job finish first.")
            return
        tier = tier or self.tier
        if tier not in TIER_ORDER or not self.results:
            self.status.set("Pick a row on one of the route tabs first.")
            return
        if item is None:
            sel = self.trees[tier].selection()
            rows = self.rows_on_screen.get(tier, [])
            if not sel or int(sel[0]) >= len(rows):
                self.status.set("Select a row first, then Refresh this item.")
                return
            item = rows[int(sel[0])]

        source = next((r for r in self.results["tables"][tier]
                       if r["id"] == item["id"] and r["q"] == item["q"]), None)
        if source is None:
            self.status.set("That row isn't in the current scan any more.")
            return

        meta = {"n": source["n"], "s": source["st"], "c": source["c"]}
        hq = source["q"] == "HQ"
        self.status.set(f"Re-pricing {source['n']}…")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))
        self.progress["value"] = 0

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def work():
            try:
                geo = engine.load_geo()
                fresh = engine.refresh_one(source["id"], hq, meta, tier, geo,
                                           report, self.stop_flag.is_set)
                self.queue.put(("rowdone", (tier, source["id"], source["q"], fresh),
                                None))
            except Exception as exc:
                self.queue.put(("lookupfail", str(exc), None))

        self.stop_flag.clear()
        self.job = "row"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _row_refreshed(self, tier, item_id, quality, fresh):
        """Swap the new row in, or drop it if it's no longer worth trading."""
        table = self.results["tables"][tier]
        idx = next((i for i, r in enumerate(table)
                    if r["id"] == item_id and r["q"] == quality), None)
        if idx is None:
            return
        name = table[idx]["n"]
        if fresh is None:
            # Every world failed the margin test on fresh numbers. Saying so is
            # more useful than leaving a stale row that looks like a trade.
            table.pop(idx)
            self.fill()
            self.status.set(f"{name} is no longer worth trading at current "
                            f"prices — removed from this table.")
            return
        world = self.world.get(tier)
        before = table[idx]["w"].get(world, {}).get("p")
        table[idx] = fresh
        self.fill()
        after = fresh["w"].get(world, {}).get("p")
        if before is not None and after is not None and before != after:
            move = after - before
            self.status.set(
                f"{name} re-priced: {after:,} a unit on {world}, "
                f"{'up' if move > 0 else 'down'} {abs(move):,} since the scan.")
        elif after is None:
            self.status.set(f"{name} re-priced — no longer worth selling on "
                            f"{world}, but other worlds may still work.")
        else:
            self.status.set(f"{name} re-priced — unchanged on {world}.")

    # -- settings ----------------------------------------------------------

    def open_settings(self):
        """Settings live in their own window, opened from the menu bar."""
        existing = getattr(self, "_settings_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=BG)
        win.geometry(f"{self.px(760)}x{self.px(620)}")
        win.transient(self)
        self._settings_win = win
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=18, pady=14)
        self._settings_tab(frame)
        ttk.Button(win, text="Close", command=win.destroy).pack(
            anchor="e", padx=18, pady=(0, 14))
        win.bind("<Escape>", lambda e: win.destroy())

    def open_data_folder(self):
        import subprocess
        try:
            if sys.platform == "win32":
                os.startfile(paths.DATA_DIR)        # noqa: S606
            else:
                subprocess.Popen(["xdg-open", paths.DATA_DIR])
        except OSError as exc:
            messagebox.showinfo("Data folder",
                                f"{paths.DATA_DIR}\n\n(couldn't open it: {exc})")

    def _settings_tab(self, parent):
        ttk.Label(parent, style="Dim.TLabel", wraplength=1320, justify="left",
                  text="Colours are set by role rather than by widget, so a "
                       "change stays consistent everywhere. Positive and warning "
                       "colours are kept separate from the accent on purpose — "
                       "they carry meaning, and shouldn't end up the same hue as "
                       "a heading."
                  ).pack(anchor="w", pady=(4, 10))

        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="PRESET", style="Dim.TLabel").pack(side="left",
                                                               padx=(0, 8))
        self.preset_var = tk.StringVar(value="")
        box = ttk.Combobox(top, textvariable=self.preset_var, width=16,
                           state="readonly", values=list(store.PRESETS),
                           font=self.f_body)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self._use_preset())
        ttk.Button(top, text="Reset to default",
                   command=self._reset_theme).pack(side="right")
        self.theme_note = ttk.Label(top, style="Dim.TLabel")
        self.theme_note.pack(side="left", padx=(16, 0))

        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        self.swatches = {}
        for i, (key, label, _default) in enumerate(store.ROLES):
            row, col = divmod(i, 2)
            cell = ttk.Frame(grid)
            cell.grid(row=row, column=col, sticky="ew", padx=(0, 30), pady=5)
            grid.columnconfigure(col, weight=1)
            ttk.Label(cell, text=label, style="TLabel", width=20).pack(side="left")
            var = tk.StringVar(value=self.theme[key])
            entry = ttk.Entry(cell, textvariable=var, width=10, font=self.f_mono
                              if hasattr(self, "f_mono") else self.f_body)
            entry.pack(side="left", padx=(0, 8))
            chip = tk.Frame(cell, width=self.px(46), height=self.px(22),
                            bg=self.theme[key], highlightthickness=1,
                            highlightbackground=LINE)
            chip.pack(side="left")
            chip.pack_propagate(False)
            self.swatches[key] = (var, chip)
            var.trace_add("write", lambda *_a, k=key: self._colour_typed(k))

        ttk.Label(parent, style="Dim.TLabel", wraplength=self.px(700),
                  justify="left",
                  text="Type any #rrggbb value. Changes apply as you type and "
                       "save themselves."
                  ).pack(anchor="w", pady=(14, 0))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=(16, 14))
        ttk.Label(parent, text="MY RETAINERS", style="Dim.TLabel").pack(anchor="w")
        ttk.Label(parent, style="Dim.TLabel", wraplength=self.px(700),
                  justify="left",
                  text="Universalis publishes the retainer name on every listing, "
                       "so telling the app yours lets it point out which rows on a "
                       "market board are you. Separate names with commas. This is "
                       "stored on this machine and never sent anywhere."
                  ).pack(anchor="w", pady=(2, 8))
        add = ttk.Frame(parent)
        add.pack(fill="x", anchor="w")
        ttk.Label(add, text="Name", style="Dim.TLabel").pack(side="left", padx=(0, 6))
        self.retainer_var = tk.StringVar()
        entry = ttk.Entry(add, textvariable=self.retainer_var, width=20,
                          font=self.f_body)
        entry.pack(side="left", padx=(0, 12))
        ttk.Label(add, text="World", style="Dim.TLabel").pack(side="left", padx=(0, 6))
        self.retainer_world = tk.StringVar()
        self.retainer_box = ttk.Combobox(add, textvariable=self.retainer_world,
                                         width=16, state="readonly",
                                         values=self._known_worlds(),
                                         font=self.f_body)
        self.retainer_box.pack(side="left", padx=(0, 12))
        ttk.Button(add, text="Add", command=self._add_retainer).pack(side="left")
        entry.bind("<Return>", lambda e: self._add_retainer())

        self.retainer_list = ttk.Treeview(
            parent, columns=("name", "world"), show="headings", height=6,
            selectmode="browse")
        self.retainer_list.heading("name", text="Retainer")
        self.retainer_list.heading("world", text="World")
        self.retainer_list.column("name", width=self.px(200), anchor="w")
        self.retainer_list.column("world", width=self.px(140), anchor="w")
        self.retainer_list.tag_configure("odd", background=PANEL_2)
        self.retainer_list.tag_configure("bad", foreground=WARN)
        self.retainer_list.pack(anchor="w", fill="x", pady=(8, 0))
        ttk.Button(parent, text="Remove selected",
                   command=self._remove_retainer).pack(anchor="w", pady=(6, 0))
        self.retainer_note = ttk.Label(parent, style="Dim.TLabel")
        self.retainer_note.pack(anchor="w", pady=(6, 0))
        self._draw_retainers()
        self._check_contrast()

    def _known_worlds(self):
        """Every world the app has seen, for the retainer picker."""
        worlds = set()
        for menu in (self.results or {}).get("menus", {}).values():
            for names in menu.values():
                worlds.update(names)
        if not worlds:
            try:
                geo = engine.load_geo()
                for side in ("oce", "na"):
                    for dc in geo[side].values():
                        worlds.update(dc.values())
            except Exception:
                pass
        return sorted(worlds)

    def _add_retainer(self):
        name = self.retainer_var.get().strip()
        world = self.retainer_world.get().strip()
        if not name:
            self.retainer_note.configure(text="type a retainer name first")
            return
        if not world:
            self.retainer_note.configure(
                text="pick the world that retainer lives on — names are only "
                     "unique within a world")
            return
        current = store.clean_retainers(self.settings.get("retainers"))
        current.append({"name": name, "world": world})
        self.settings["retainers"] = store.clean_retainers(current)
        store.save_settings(self.settings)
        self.retainer_var.set("")
        self._draw_retainers()
        self.fill()

    def _remove_retainer(self):
        sel = self.retainer_list.selection()
        if not sel:
            return
        keep = [r for i, r in enumerate(
            store.clean_retainers(self.settings.get("retainers")))
            if str(i) not in sel]
        self.settings["retainers"] = keep
        store.save_settings(self.settings)
        self._draw_retainers()
        self.fill()

    def _draw_retainers(self):
        rows = store.clean_retainers(self.settings.get("retainers"))
        self.settings["retainers"] = rows
        tree = self.retainer_list
        tree.delete(*tree.get_children())
        missing = 0
        for i, r in enumerate(rows):
            tags = ["odd"] if i % 2 else []
            if not r["world"]:
                tags.append("bad")
                missing += 1
            tree.insert("", "end", iid=str(i), tags=tags,
                        values=(r["name"], r["world"] or "— set a world —"))
        if not rows:
            note = "none set — your own listings won't be marked"
        elif missing:
            note = (f"{missing} of {len(rows)} have no world set. Those are matched "
                    f"on every world, which can claim someone else's listing — "
                    f"remove and re-add them with the right world.")
        else:
            note = f"{len(rows)} retainer{'' if len(rows) == 1 else 's'} set"
        self.retainer_note.configure(text=note)

    def _colour_typed(self, key):
        var, chip = self.swatches[key]
        value = var.get().strip()
        if not store.valid_hex(value):
            self.theme_note.configure(text=f"{value} isn't a #rrggbb colour")
            return
        self.theme[key] = value
        try:
            chip.configure(bg=value)
        except tk.TclError:
            return
        store.save_theme(self.theme)
        self._repaint()

    def _use_preset(self):
        name = self.preset_var.get()
        if name not in store.PRESETS:
            return
        self.theme = dict(store.PRESETS[name])
        store.save_theme(self.theme)
        self._sync_swatches()
        self._repaint()

    def _reset_theme(self):
        store.reset_theme()
        self.theme = store.load_theme()
        self.preset_var.set("")
        self._sync_swatches()
        self._repaint()

    def _sync_swatches(self):
        for key, (var, chip) in self.swatches.items():
            var.set(self.theme[key])
            try:
                chip.configure(bg=self.theme[key])
            except tk.TclError:
                pass

    def _repaint(self):
        """Rebind the palette and rebuild every style in place."""
        apply_theme(self.theme)
        self.configure(bg=BG)
        self._style()
        for tree in list(self.trees.values()) + [self.basket_tree,
                                                 self.positions_tree,
                                                 self.find_tree]:
            tree.tag_configure("odd", background=PANEL_2)
            tree.tag_configure("free", foreground=GOOD)
            tree.tag_configure("picked", foreground=GOLD)
            tree.tag_configure("good", foreground=GOOD)
            tree.tag_configure("bad", foreground=WARN)
            tree.tag_configure("loss", foreground=INK_3)
            tree.tag_configure("group", foreground=GOLD, background=PANEL_2)
        try:
            if self.suggest_list is not None:
                self.suggest_list.configure(
                    bg=PANEL, fg=INK, highlightbackground=LINE,
                    selectbackground=_mix(PANEL, GOLD, 0.25),
                    selectforeground=GOLD)
        except (AttributeError, tk.TclError):
            pass
        self.banner.configure(style="Alert.TFrame")
        self._check_contrast()
        if self.results:
            self.fill()
        else:
            self._strip()

    def _check_contrast(self):
        """Warn when a palette makes text hard to read, rather than allowing it silently."""
        def lum(hexv):
            parts = []
            for i in (1, 3, 5):
                c = int(hexv[i:i + 2], 16) / 255
                parts.append(c / 12.92 if c <= 0.03928
                             else ((c + 0.055) / 1.055) ** 2.4)
            return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

        def ratio(a, b):
            try:
                l1, l2 = sorted((lum(a), lum(b)), reverse=True)
            except (ValueError, IndexError):
                return 21.0
            return (l1 + 0.05) / (l2 + 0.05)

        problems = []
        for fg, bg, what in (("ink", "surface", "main text"),
                             ("ink2", "surface", "secondary text"),
                             ("ink3", "surface2", "table headings"),
                             ("accent", "surface2", "accent"),
                             ("good", "surface", "positive"),
                             ("warn", "surface", "warnings")):
            r = ratio(self.theme[fg], self.theme[bg])
            if r < 4.5:
                problems.append(f"{what} {r:.1f}:1")
        self.theme_note.configure(
            text=("hard to read: " + ", ".join(problems) + "  (4.5:1 is the minimum)"
                  if problems else "all text passes 4.5:1 contrast"))

    # -- item lookup -------------------------------------------------------

    def _search_tab(self, parent):
        ttk.Label(parent, style="Dim.TLabel", wraplength=1320, justify="left",
                  text="Price one item on every world in both regions, in a few "
                       "seconds, with none of the filters the route tables apply "
                       "— so an item being griefed or currently unprofitable "
                       "still shows, which is exactly when you want to look."
                  ).pack(anchor="w", pady=(4, 8))

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="ITEM", style="Dim.TLabel").pack(side="left", padx=(0, 8))
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(bar, textvariable=self.find_var, width=38,
                                    font=self.f_body)
        self.find_entry.pack(side="left")
        self.find_entry.bind("<Down>", self._suggest_down)
        self.find_entry.bind("<Up>", self._suggest_up)
        self.find_entry.bind("<Return>", self._suggest_take)
        self.find_entry.bind("<Escape>", lambda e: self._hide_suggest())
        self.find_entry.bind("<FocusOut>",
                             lambda e: self.after(150, self._hide_suggest))
        self.find_var.trace_add("write", lambda *_: self._suggest())
        self.find_btn = ttk.Button(bar, text="Look up prices", style="Go.TButton",
                                   command=self.run_lookup)
        self.find_btn.pack(side="left", padx=(8, 0))
        self.find_note = ttk.Label(bar, style="Dim.TLabel")
        self.find_note.pack(side="left", padx=(14, 0))

        # The suggestion list floats over the table rather than sitting beside
        # it, so it behaves like the autocomplete people expect: type, arrow
        # down, Enter. Item names are unforgiving about spelling.
        self.suggest_win = None
        self.suggest_list = None

        self.find_head = ttk.Label(parent, style="Dim.TLabel", justify="left",
                                   wraplength=1320)
        self.find_head.pack(anchor="w", pady=(10, 6))

        cols = (("region", "Region", 120, "w"), ("world", "World", 110, "w"),
                ("q", "Q", 34, "center"), ("buy", "Cheapest here", 105, "e"),
                ("units", "For sale", 75, "e"), ("sell", "Sells for", 100, "e"),
                ("rate", "Sells / day", 85, "e"), ("sold", "Sold", 60, "e"),
                ("net", "Net / unit", 95, "e"), ("margin", "Margin", 70, "e"))
        tree = ttk.Treeview(parent, columns=[c[0] for c in cols],
                            show="tree headings", selectmode="browse")
        tree.column("#0", width=self.px(16), stretch=False)
        for key, label, width, anchor in cols:
            tree.heading(key, text=label)
            tree.column(key, width=self.px(width), anchor=anchor,
                        stretch=(key == "region"))
        tree.tag_configure("odd", background=PANEL_2)
        tree.tag_configure("group", foreground=GOLD, background=PANEL_2)
        tree.tag_configure("best", foreground=GOOD)
        tree.tag_configure("loss", foreground=INK_3)
        tree.tag_configure("cheap", foreground=GOLD)
        tree.bind("<Double-1>", lambda e: self.show_lookup_detail())
        tree.bind("<Return>", lambda e: self.show_lookup_detail())
        tree.bind("<space>", lambda e: self.show_lookup_detail())
        tree.pack(fill="both", expand=True)
        self.find_tree = tree
        self.lookup_rows = {}
        self._refresh_matches()

    # -- autocomplete ------------------------------------------------------

    def _matches_for(self, text):
        if not self.name_index:
            return []
        return engine.search_names(self.name_index, text, limit=12)

    def _suggest(self):
        """Drop a list of matching item names under the box as you type."""
        self.found = self._matches_for(self.find_var.get())
        if not self.name_index:
            self.find_note.configure(
                text="press Look up to build the item index first (about a minute)")
            self._hide_suggest()
            return
        typed = self.find_var.get().strip()
        self.find_note.configure(
            text=(f"{len(self.name_index):,} items indexed" if len(typed) < 2
                  else f"{len(self.found)} match"
                       f"{'' if len(self.found) == 1 else 'es'}"))
        if not self.found or len(typed) < 2:
            self._hide_suggest()
            return
        # An exact hit needs no menu in the way.
        if len(self.found) == 1 and self.found[0][1].lower() == typed.lower():
            self._hide_suggest()
            return
        self._show_suggest()

    def _show_suggest(self):
        if self.suggest_win is None or not self.suggest_win.winfo_exists():
            self.suggest_win = tk.Toplevel(self)
            self.suggest_win.overrideredirect(True)
            self.suggest_win.attributes("-topmost", True)
            self.suggest_list = tk.Listbox(
                self.suggest_win, font=self.f_body, bg=PANEL, fg=INK,
                selectbackground=_mix(PANEL, GOLD, 0.25), selectforeground=GOLD,
                highlightthickness=1, highlightbackground=LINE,
                borderwidth=0, activestyle="none")
            self.suggest_list.pack(fill="both", expand=True)
            self.suggest_list.bind("<Button-1>",
                                   lambda e: self.after(1, self._suggest_take))
            self.suggest_list.bind("<Return>", self._suggest_take)
        self.suggest_list.delete(0, "end")
        for _iid, name in self.found:
            self.suggest_list.insert("end", name)
        self.suggest_list.selection_clear(0, "end")
        self.suggest_list.selection_set(0)
        rows = min(len(self.found), 10)
        x = self.find_entry.winfo_rootx()
        y = self.find_entry.winfo_rooty() + self.find_entry.winfo_height()
        width = max(self.find_entry.winfo_width(), self.px(320))
        self.suggest_win.geometry(f"{width}x{rows * self.px(21) + 4}+{x}+{y}")
        self.suggest_win.deiconify()
        self.suggest_list.configure(height=rows)

    def _hide_suggest(self):
        if self.suggest_win is not None and self.suggest_win.winfo_exists():
            self.suggest_win.withdraw()

    def _suggest_move(self, delta):
        if not (self.suggest_win and self.suggest_win.winfo_exists()
                and self.suggest_win.winfo_viewable()):
            return
        size = self.suggest_list.size()
        if not size:
            return
        cur = (self.suggest_list.curselection() or (0,))[0]
        nxt = max(0, min(size - 1, cur + delta))
        self.suggest_list.selection_clear(0, "end")
        self.suggest_list.selection_set(nxt)
        self.suggest_list.see(nxt)

    def _suggest_down(self, _e=None):
        self._suggest_move(1)
        return "break"

    def _suggest_up(self, _e=None):
        self._suggest_move(-1)
        return "break"

    def _suggest_take(self, _e=None):
        """Put the highlighted suggestion in the box, then look it up."""
        if (self.suggest_win and self.suggest_win.winfo_exists()
                and self.suggest_win.winfo_viewable()):
            sel = self.suggest_list.curselection()
            if sel and sel[0] < len(self.found):
                name = self.found[sel[0]][1]
                self._hide_suggest()
                self.find_var.set(name)
                self.find_entry.icursor("end")
                self._hide_suggest()
                self.run_lookup()
                return "break"
        self._hide_suggest()
        self.run_lookup()
        return "break"

    def _refresh_matches(self):
        self.found = self._matches_for(self.find_var.get())
        if not self.name_index:
            self.find_note.configure(
                text="press Look up to build the item index first (about a minute)")
        else:
            self.find_note.configure(text=f"{len(self.name_index):,} items indexed")

    def run_lookup(self):
        if self.worker and self.worker.is_alive():
            self.status.set("Already busy — let the current job finish first.")
            return
        if not self.name_index:
            self._build_index()
            return
        typed = self.find_var.get().strip()
        exact = [(i, n) for i, n in (self.found or [])
                 if n.lower() == typed.lower()]
        pick = exact or self.found
        if not pick:
            self.status.set("No item matches that. Check the spelling — the "
                            "dropdown shows what's available as you type.")
            return
        item_id, name = pick[0]
        self.status.set(f"Pricing {name} on every world…")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))
        self.progress["value"] = 0

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def work():
            try:
                geo = engine.load_geo()
                res = engine.lookup_item(item_id, geo, report,
                                         self.stop_flag.is_set)
                self.queue.put(("lookup", (name, res), None))
            except Exception as exc:
                self.queue.put(("lookupfail", str(exc), None))

        self.stop_flag.clear()
        self.job = "lookup"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _build_index(self):
        self.status.set("Building the item index — about a minute, once only.")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def work():
            try:
                F = engine.Fetcher(report, self.stop_flag.is_set)
                self.queue.put(("index", engine.build_name_index(F, report), None))
            except Exception as exc:
                self.queue.put(("lookupfail", str(exc), None))

        self.stop_flag.clear()
        self.job = "index"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _show_lookup(self, name, res):
        """One row per world, grouped by region, both qualities."""
        tree = self.find_tree
        tree.delete(*tree.get_children())
        self.lookup_rows = {}
        notes = []

        for quality in ("HQ", "NQ"):
            blob = res.get(quality)
            if not blob:
                continue
            cheapest, dearest, best = (blob.get("cheapest"), blob.get("dearest"),
                                       blob.get("best"))
            if cheapest:
                notes.append(f"{quality}: cheapest {cheapest['buy']:,} on "
                             f"{cheapest['world']}")
            if best and best.get("net", 0) > 0:
                notes.append(f"best {best['net']:,}/unit selling on "
                             f"{best['world']} ({best['margin']}%)")

            by_region = {}
            for r in blob["rows"]:
                by_region.setdefault(r["region"], []).append(r)

            for region in ("North America", "Materia"):
                rows = by_region.get(region)
                if not rows:
                    continue
                live = [r for r in rows if r["buy"] is not None]
                cheap_here = min((r["buy"] for r in live), default=None)
                parent = tree.insert(
                    "", "end", open=True, tags=("group",),
                    values=(f"{region} — {quality}", f"{len(rows)} worlds", "",
                            f"{cheap_here:,}" if cheap_here else "—",
                            f"{sum(r['units'] for r in rows):,}", "", "",
                            f"{sum(r['sold'] for r in rows):,}", "", ""))
                for i, r in enumerate(sorted(rows, key=lambda x: -(x["net"] or -10**9))):
                    tags = ["odd"] if i % 2 else []
                    if best and r is best:
                        tags.append("best")
                    elif cheapest and r is cheapest:
                        tags.append("cheap")
                    elif r["net"] is not None and r["net"] <= 0:
                        tags.append("loss")
                    node = tree.insert(parent, "end", tags=tags, values=(
                        "", r["world"], quality,
                        f"{r['buy']:,}" if r["buy"] else "— none —",
                        f"{r['units']:,}" if r["units"] else "—",
                        f"{r['sell']:,}" if r["sell"] else "—",
                        fmt("rate", r["rate"]) if r["rate"] else "—",
                        f"{r['sold']:,}" if r["sold"] else "—",
                        f"{r['net']:,}" if r["net"] is not None else "—",
                        f"{r['margin']}%" if r["margin"] is not None else "—"))
                    self.lookup_rows[node] = dict(r, name=name, q=quality)

        self.find_head.configure(
            text=f"{name}\n" + "     ".join(notes) if notes else name)
        hq = res.get("HQ", {}).get("best")
        nq = res.get("NQ", {}).get("best")
        top = max((b for b in (hq, nq) if b), key=lambda b: b["net"], default=None)
        if top and top["net"] > 0:
            self.status.set(f"{name}: best is {top['net']:,} a unit selling on "
                            f"{top['world']} ({top['region']}).")
        else:
            self.status.set(f"{name}: nothing profitable anywhere right now.")

    def show_lookup_detail(self):
        """The board behind a row in the lookup results.

        The route tabs let you open a row and see the wall; this is the same
        thing for a world you found through search, so the two halves of the
        app behave alike instead of one of them being a dead end.
        """
        sel = self.find_tree.selection()
        if not sel:
            return
        row = self.lookup_rows.get(sel[0])
        if not row:
            return          # a region heading, not a world

        win = tk.Toplevel(self)
        win.title(f"{row['name']} - {row['world']}")
        win.configure(bg=BG)
        win.geometry(f"{self.px(760)}x{self.px(640)}")
        win.transient(self)

        head = ttk.Frame(win)
        head.pack(fill="x", padx=18, pady=(16, 0))
        ttk.Label(head, text=row["name"], style="Head.TLabel").pack(anchor="w")
        ttk.Label(head, style="Dim.TLabel",
                  text=f"{row['q']} · {row['world']} · {row['region']} · "
                       f"{row['dc']} data centre").pack(anchor="w", pady=(2, 0))

        facts = ttk.Frame(win, style="Panel.TFrame")
        facts.pack(fill="x", padx=18, pady=(14, 0))
        cells = (
            ("CHEAPEST HERE",
             f"{row['buy']:,}" if row["buy"] else "nothing listed",
             f"{row['units']:,} units in {row['listings']} listings"
             if row["units"] else "no stock on this world"),
            ("SELLS FOR", f"{row['sell']:,}" if row["sell"] else "—",
             f"{row['sold']:,} sold · {fmt('rate', row['rate'])}/day"),
            ("NET / UNIT", f"{row['net']:,}" if row["net"] is not None else "—",
             f"{row['margin']}% buying at the cheapest board"
             if row["margin"] is not None else "needs a price on both sides"),
        )
        for i, (k, v, note) in enumerate(cells):
            cell = ttk.Frame(facts, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            facts.columnconfigure(i, weight=1, uniform="lfacts")
            ttk.Label(cell, text=k, style="Key.TLabel").pack(anchor="w", padx=12,
                                                             pady=(10, 1))
            ttk.Label(cell, text=v, style="Val.TLabel").pack(anchor="w", padx=12)
            ttk.Label(cell, text=note, style="Note.TLabel", wraplength=self.px(200)
                      ).pack(anchor="w", padx=12, pady=(1, 10))

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 0))

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 9))
        ttk.Label(left, text=f"THE WALL ON {row['world'].upper()}",
                  style="Dim.TLabel").pack(anchor="w")
        wall = row.get("wall") or []
        wtree = ttk.Treeview(left, columns=("p", "q", "r"), show="headings",
                             height=9)
        for key, label, width, anchor_ in (("p", "Price", 100, "e"),
                                           ("q", "Qty", 50, "e"),
                                           ("r", "Retainer", 140, "w")):
            wtree.heading(key, text=label)
            wtree.column(key, width=self.px(width), anchor=anchor_)
        wtree.tag_configure("odd", background=PANEL_2)
        wtree.tag_configure("picked", foreground=GOLD)
        yours = store.retainers_on(self.settings.get("retainers"), row["world"])
        mine_here = 0
        for i, pair in enumerate(wall):
            who = (pair[2] if len(pair) > 2 else None) or ""
            is_mine = bool(yours) and who.lower() in yours
            mine_here += 1 if is_mine else 0
            tags = ["odd"] if i % 2 else []
            if is_mine:
                tags.append("picked")
            wtree.insert("", "end", tags=tags,
                         values=(f"{pair[0]:,}", f"{pair[1]:,}",
                                 (who + "  <- you") if is_mine else who))
        if not wall:
            wtree.insert("", "end", values=("- none listed -", "", ""))
        wtree.pack(fill="both", expand=True, pady=(6, 0))
        note = (f"{sum(p[1] for p in wall):,} units across {len(wall)} listings."
                if wall else
                f"Nothing listed on {row['world']} - you would set the price.")
        if mine_here:
            note += f"  {mine_here} of them yours."
        ttk.Label(left, style="Dim.TLabel", wraplength=self.px(320),
                  justify="left", text=note).pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(9, 0))
        ttk.Label(right, text="WHAT IT ACTUALLY SOLD FOR",
                  style="Dim.TLabel").pack(anchor="w")
        hist = row.get("hist") or []
        canvas = tk.Canvas(right, height=self.px(150), bg=PANEL,
                           highlightthickness=0)
        canvas.pack(fill="x", pady=(6, 0))
        canvas.after(30, lambda: self._draw_history(canvas, hist,
                                                    row.get("sell") or 0))
        htree = ttk.Treeview(right, columns=("p", "q", "w"), show="headings",
                             height=6)
        for key, label, width in (("p", "Price", 100), ("q", "Qty", 50),
                                  ("w", "When", 90)):
            htree.heading(key, text=label)
            htree.column(key, width=self.px(width),
                         anchor="e" if key != "w" else "w")
        htree.tag_configure("odd", background=PANEL_2)
        for i, entry in enumerate(hist):
            ago = (time.time() - entry[2]) / 86400
            htree.insert("", "end", tags=("odd",) if i % 2 else (),
                         values=(f"{entry[0]:,}", f"{entry[1]:,}",
                                 fmt("dur", ago) + " ago"))
        if not hist:
            htree.insert("", "end", values=("no recorded sales", "", ""))
        htree.pack(fill="both", expand=True, pady=(6, 0))

        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=18, pady=14)
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="Open on Universalis",
                   command=lambda: webbrowser.open(
                       "https://universalis.app/market/%d" % row["id"])).pack(
                           side="right", padx=(0, 8))
        win.bind("<Escape>", lambda e: win.destroy())

    # -- auto refresh ------------------------------------------------------

    def _paint_auto(self):
        on = self.settings["auto_refresh"]
        hours = self.settings["auto_refresh_hours"]
        self.auto_btn.configure(
            text=f"Auto-refresh: {'on' if on else 'off'}",
            style="On.TButton" if on else "Off.TButton")
        tip = (f"Re-scans on its own once the data is over {hours} hours old"
               if on else "Only refreshes when you ask it to")
        self.auto_btn.configure(cursor="hand2")
        self.auto_tip = tip

    def toggle_auto(self):
        self.settings["auto_refresh"] = not self.settings["auto_refresh"]
        store.save_settings(self.settings)
        self._paint_auto()
        self.status.set(self.auto_tip + ".")
        if self.settings["auto_refresh"]:
            self._auto_check()

    def _auto_check(self):
        """Start a scan if auto-refresh is on and the data has aged out."""
        if not self.settings["auto_refresh"]:
            return
        if self.worker and self.worker.is_alive():
            return
        age = engine.snapshot_age_hours()
        if age is not None and age >= self.settings["auto_refresh_hours"]:
            self.status.set(f"Data is {age_text(age)} — refreshing automatically.")
            self.refresh()

    def _auto_tick(self):
        self._auto_check()
        self._staleness()
        self.after(10 * 60 * 1000, self._auto_tick)

    def _staleness(self):
        age = engine.snapshot_age_hours()
        if age is None or not self.results:
            return
        if age >= 24 and not (self.worker and self.worker.is_alive()):
            self.show_banner(
                f"This scan is {age_text(age)}. Market prices move a lot in that "
                f"time — refresh before acting on it.", self.refresh, "Refresh now")

    # -- tabs / world picker ----------------------------------------------

    def _tab_changed(self, _event=None):
        idx = self.notebook.index(self.notebook.select())
        if TAB_NAMES[idx] == "My listings":
            self._auto_find_mine()
        if idx >= len(TIER_ORDER):
            self.sell_label.configure(text="SHOPPING LIST")
            for child in self.world_box.winfo_children():
                child.destroy()
            self.subtitle.configure(
                text="Everything you've ticked, grouped by the world you buy it on")
            self.draw_basket()
            self._strip()
            return
        self.tier = TIER_ORDER[idx]
        self.sell_label.configure(text="SELLING ON")
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

    def world_options(self, tier):
        menu = (self.results or {}).get("menus", {}).get(tier, {})
        return [w for dc in sorted(menu) for w in menu[dc]]

    def _world_picker(self):
        """Buttons for a handful of worlds, dropdowns for thirty-two."""
        for child in self.world_box.winfo_children():
            child.destroy()
        if not self.results:
            # The world list comes from the scan, so before the first one there
            # is nothing to offer. Say that rather than showing a blank gap that
            # reads as a missing feature.
            ttk.Label(self.world_box, style="Dim.TLabel",
                      text="pick your world once the first scan finishes").pack(
                          side="left")
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
        self.settings["worlds"] = dict(self.world)
        store.save_settings(self.settings)
        if len(self.world_options(self.tier)) <= 8:
            self._world_picker()
        self.fill()

    # -- data --------------------------------------------------------------

    def _load_cache(self):
        if self.settings.get("check_updates_on_open", True):
            # A moment after the window is up, so it never delays the app
            # appearing, and silent unless there is genuinely something newer.
            # Nobody wants a dialog on every launch telling them they're fine.
            self.after(2500, lambda: self.check_update(quiet=True))
        cached = engine.load_cached()
        if cached:
            self.apply(cached, note="from last scan")
            self._probe()
            if self.settings["refresh_on_open"]:
                age = engine.snapshot_age_hours()
                if age is None or age >= self.settings["auto_refresh_hours"]:
                    self.after(1500, self.refresh)
            return

        # Nothing cached at all. Don't present an empty grid and wait to be
        # asked -- that is indistinguishable from a broken app. Go and get it.
        self._subtitle()
        self.status.set("First run — fetching prices from Universalis. "
                        "This takes about five minutes.")
        self.show_banner(
            "Nothing has been scanned on this machine yet, so the tables are "
            "empty. Fetching prices now — it takes around five minutes, and "
            "after this the app opens on the last scan instantly.",
            None)
        self.after(600, self._first_scan)

    def _first_scan(self):
        """Check we can reach the API before promising a five-minute scan."""
        def work():
            self.queue.put(("firstrun", engine.health(), None))
        threading.Thread(target=work, daemon=True).start()

    def _probe(self):
        def work():
            self.queue.put(("health", engine.health(), None))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_flag.clear()
        self.hide_banner()
        self.refresh_btn.pack_forget()
        self.cancel_btn.pack(side="right")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))
        self.progress["value"] = 0
        self.status.set("Starting…")

        self.scan_started = time.time()

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def work():
            try:
                self.queue.put(("done", engine.run_scan(report, self.stop_flag.is_set),
                                None))
            except engine.Cancelled:
                self.queue.put(("cancelled", None, None))
            except engine.Unreachable as exc:
                self.queue.put(("unreachable", str(exc), None))
            except Exception as exc:                      # API shape, disk, etc.
                self.queue.put(("error", exc, None))

        self.job = "scan"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.is_alive():
            self.stop_flag.set()
            self.status.set("Stopping — keeping whatever has been fetched so far.")

    def focus_filter(self):
        self.filter_entry.focus_set()
        self.filter_entry.select_range(0, "end")

    def _pump(self):
        try:
            while True:
                kind, payload, frac = self.queue.get_nowait()
                if kind == "progress":
                    self.status.set(payload + self._eta(frac))
                    if frac is not None:
                        self.progress["value"] = frac * 1000
                elif kind == "done":
                    self._end_scan()
                    self.hide_banner()
                    part = payload.get("partial")
                    self.apply(payload,
                               note="partial" if part else "just now")
                    if part:
                        self.show_banner(
                            f"Stopped early — {part['covered']} of {part['of']} "
                            f"items were priced, so these tables are incomplete. "
                            f"Your last full scan is still saved and will come "
                            f"back next time you open the app.",
                            self.refresh, "Run a full scan")
                elif kind == "cancelled":
                    self._end_scan()
                    self.status.set("Scan stopped. Showing the previous data.")
                elif kind == "unreachable":
                    self._end_scan()
                    self.status.set("Couldn't reach Universalis.")
                    stale = (f" Still showing the scan from {self.results['stamp']}."
                             if self.results else
                             " There's no earlier data to fall back on.")
                    self.show_banner(f"{payload}{stale}", self.refresh, "Try again")
                elif kind == "error":
                    checking = self.job == "positions"
                    self._hide_progress() if checking else self._end_scan()
                    self.status.set("Check failed." if checking else "Scan failed.")
                    self.show_banner(
                        ("Checking your listings stopped with an unexpected error: "
                         if checking else
                         "The scan stopped with an unexpected error: ") + str(payload),
                        self.check_positions if checking else self.refresh,
                        "Try again")
                elif kind == "firstrun":
                    ok, message = payload
                    if ok:
                        self.refresh()
                    else:
                        self.status.set("Couldn't reach Universalis.")
                        self.show_banner(
                            message + " There's no earlier scan to fall back on, "
                            "so the tables stay empty until this works.",
                            self._first_scan, "Try again")
                elif kind == "health":
                    ok, msg = payload
                    if not ok:
                        self.show_banner(
                            msg + (" Showing the last scan instead."
                                   if self.results else ""),
                            self._probe, "Check again")
                    else:
                        self.hide_banner()
                        self._staleness()
                elif kind == "positions":
                    self._hide_progress()
                    self._positions_result(payload)
                elif kind == "rowdone":
                    self._hide_progress()
                    self._row_refreshed(*payload)
                elif kind == "board":
                    self._show_board(*payload)
                elif kind == "swept":
                    self._hide_progress()
                    self._swept(*payload)
                elif kind == "lookup":
                    self._hide_progress()
                    self._show_lookup(*payload)
                elif kind == "index":
                    self._hide_progress()
                    self.name_index = payload
                    self._refresh_matches()
                    self.status.set(f"Indexed {len(payload):,} items — "
                                    f"search works instantly from now on.")
                elif kind == "lookupfail":
                    self._hide_progress()
                    self.status.set("Lookup failed.")
                    self.show_banner(str(payload), self.run_lookup, "Try again")
                elif kind == "update":
                    self._update_result(payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _eta(self, frac):
        """How much longer, from how long the scan has taken to get this far.

        Held back until 4% done -- before that the estimate swings wildly and a
        number that jumps from 40 minutes to 3 is worse than no number at all.
        """
        started = getattr(self, "scan_started", None)
        if not started or not frac or frac < 0.04 or frac >= 0.99:
            return ""
        remaining = (time.time() - started) / frac * (1 - frac)
        if remaining < 45:
            return "  ·  nearly done"
        if remaining < 90:
            return "  ·  about a minute left"
        return f"  ·  about {round(remaining / 60)} min left"

    def _end_scan(self):
        """Finish a full scan: hide progress and swap Stop back for Refresh."""
        self.progress.pack_forget()
        self.cancel_btn.pack_forget()
        self.refresh_btn.pack(side="right")

    def _hide_progress(self):
        """Finish a listings check, which never replaced the Refresh button."""
        self.progress.pack_forget()

    def apply(self, results, note=""):
        self.results = results
        for tier in TIER_ORDER:
            options = self.world_options(tier)
            if options and self.world.get(tier) not in options:
                self.world[tier] = options[0]
        self._world_picker()
        self._subtitle()
        self.fill()
        self.discovered = []
        self._auto_find_mine(announce=False)
        bits = [f"Data {note} — scanned {results['stamp']}"]
        if results.get("degraded"):
            bits.append("some requests failed, so parts may be incomplete")
        elif results.get("fails"):
            bits.append(f"{results['fails']} requests failed")
        self.status.set(" · ".join(bits))

    # -- banner ------------------------------------------------------------

    def show_banner(self, text, action=None, label="Retry"):
        self.banner_text.configure(text=text)
        if action:
            self.banner_btn.configure(text=label, command=action)
            self.banner_btn.pack(side="right", padx=(0, 12), pady=8)
        else:
            self.banner_btn.pack_forget()
        self.banner.pack(fill="x", padx=18, pady=(12, 0), before=self.strip)

    def hide_banner(self):
        self.banner.pack_forget()

    # -- tables ------------------------------------------------------------

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
            if self.settings["min_margin"] and v["m"] < self.settings["min_margin"]:
                continue
            if self.settings["uncontested_only"] and v["a"]:
                continue
            gil = self.settings["budget_gil"]
            if self.settings["affordable_only"] and gil and r["b"] * v["u"] > gil:
                continue
            row = dict(v)
            row.update({"id": r["id"], "n": r["n"], "c": r["c"], "q": r["q"],
                        "st": r["st"], "b": r["b"], "nau": r["nau"],
                        "stop": r["stop"], "su": r["su"],
                        "_all": r.get("boards") or r["w"], "_sell": r["w"],
                        "_buy": r.get("buyboards") or {}})
            row["pick"] = store.key_of(row, tier, world) in self.basket
            out.append(row)
        key, desc = self.sort_state[tier]
        out.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=desc)
        return out

    def sort_by(self, tier, key):
        if key == "pick":
            return
        cur, desc = self.sort_state[tier]
        self.sort_state[tier] = (key, not desc if key == cur else key != "n")
        self.fill()

    def step_sort(self, tier, delta):
        keys = [c[0] for c in COLUMNS if c[0] != "pick"]
        cur = self.sort_state[tier][0]
        i = (keys.index(cur) + delta) % len(keys) if cur in keys else 0
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
                values = []
                for key, _h, _w, _a, kind in COLUMNS:
                    if kind == "stop":
                        values.append(f"{r['stop']} ×{r['su']:,}")
                    else:
                        values.append(fmt(kind, r.get(key)))
                tags = ["odd"] if i % 2 else []
                if r.get("pick"):
                    tags.append("picked")
                elif r.get("a") == 0:
                    tags.append("free")
                tree.insert("", "end", iid=str(i), values=values, tags=tags)
            key, desc = self.sort_state[tier]
            for k, heading, _w, _a, _kind in COLUMNS:
                arrow = "  ▼" if desc else "  ▲"
                tree.heading(k, text=heading + (arrow if k == key else ""))
        self.draw_basket()
        self._strip()

    def _strip(self):
        for child in self.strip.winfo_children():
            child.destroy()
        if not self.results:
            self.filter_note.configure(text="")
            busy = bool(self.worker and self.worker.is_alive())
            ttk.Label(self.strip, style="Note.TLabel",
                      text=("  Fetching prices — the tables fill in when it "
                            "finishes, in about five minutes."
                            if busy else
                            "  No scan yet — press Refresh data to fetch prices "
                            "from Universalis. It takes about five minutes."
                            )).pack(anchor="w", padx=14, pady=16)
            return
        idx = self.notebook.index(self.notebook.select())
        if idx >= len(TIER_ORDER):
            self._basket_totals(self.strip)
            return
        rows = self.rows_for(self.tier)
        world = self.world.get(self.tier, "—")
        age = engine.snapshot_age_hours()
        total = len(self.results["tables"].get(self.tier, []))
        hidden = total - len(rows)
        self.filter_note.configure(
            text=f"hiding {hidden} of {total} rows" if hidden else "")
        if not rows:
            ttk.Label(self.strip, style="Note.TLabel",
                      text="  Nothing tradeable on this world with the current filter."
                      ).pack(anchor="w", padx=14, pady=14)
            return
        top = max(rows, key=lambda r: r["t"])
        movers = [r for r in rows if r.get("dp")]
        rising = sum(1 for r in movers if r["dp"] > 0)
        cells = [
            ("BEST RUN HERE", f"{top['t']:,}",
             f"{top['n']} · buy {top['u']:,} · {fmt('dur', top['d'])} per sale"),
            ("UNCONTESTED LINES", f"{sum(1 for r in rows if r['a'] == 0)} / {len(rows)}",
             "nothing listed under you"),
            ("MARGINS RISING", f"{rising} / {len(movers)}" if movers else "—",
             "since the previous scan" if movers else "needs a second scan"),
            ("DATA", age_text(age), f"selling on {world} · {len(rows):,} routes"),
        ]
        for i, (k, v, n) in enumerate(cells):
            cell = ttk.Frame(self.strip, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            self.strip.columnconfigure(i, weight=1, uniform="strip")
            style = ("ValWarn.TLabel" if k == "DATA" and age and age >= 24
                     else "Val.TLabel")
            ttk.Label(cell, text=k, style="Key.TLabel").pack(anchor="w", padx=14,
                                                             pady=(12, 2))
            ttk.Label(cell, text=v, style=style).pack(anchor="w", padx=14)
            ttk.Label(cell, text=n, style="Note.TLabel", wraplength=280).pack(
                anchor="w", padx=14, pady=(2, 12))

    # -- shopping list -----------------------------------------------------

    def _table_click(self, event, tier):
        tree = self.trees[tier]
        if tree.identify("region", event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) != "#1":       # the tick column
            return
        iid = tree.identify_row(event.y)
        if iid:
            self._toggle_row(tier, int(iid))
            return "break"

    def toggle_selected(self, tier):
        sel = self.trees[tier].selection()
        if sel:
            self._toggle_row(tier, int(sel[0]))

    def _toggle_row(self, tier, index):
        rows = self.rows_on_screen.get(tier, [])
        if index >= len(rows):
            return
        store.toggle(self.basket, rows[index], tier, self.world[tier])
        store.save_basket(self.basket)
        self.fill()

    def _filters_changed(self):
        digits = "".join(ch for ch in self.margin_var.get() if ch.isdigit())
        self.settings["min_margin"] = int(digits) if digits else 0
        self.settings["affordable_only"] = self.afford_var.get()
        self.settings["uncontested_only"] = self.uncon_var.get()
        store.save_settings(self.settings)
        self.fill()

    def _budget_changed(self):
        def num(var, default=0):
            text = "".join(ch for ch in var.get() if ch.isdigit())
            return int(text) if text else default
        self.settings["budget_gil"] = num(self.gil_var)
        self.settings["retainer_slots"] = num(self.slot_var, 40)
        store.save_settings(self.settings)
        self.draw_basket()
        self._strip()

    def draw_basket(self):
        tree = self.basket_tree
        tree.delete(*tree.get_children())
        for stop, rows in store.by_stop(self.basket).items():
            sub = store.totals(self.basket, rows)
            parent = tree.insert(
                "", "end", open=True, tags=("group",),
                values=(f"Buy on {stop}", "", "", "", f"{sub['spend']:,}", "", "",
                        f"{sub['gain']:,}", f"{sub['lines']} lines"))
            for i, r in enumerate(rows):
                qty, buy = r.get("qty", 0), r.get("buy", 0)
                profit, sell = r.get("profit", 0), r.get("sell", 0)
                tree.insert(parent, "end", tags=("odd",) if i % 2 else (),
                            values=(r.get("name", "?"), r.get("q", ""), f"{qty:,}",
                                    f"{buy:,}", f"{buy * qty:,}", f"{sell:,}",
                                    f"{profit:,}", f"{profit * qty:,}",
                                    r.get("world", "?")))
        self._basket_totals(self.basket_strip)

    def _basket_totals(self, parent):
        for child in parent.winfo_children():
            child.destroy()
        t = store.totals(self.basket)
        gil = self.settings["budget_gil"]
        slots = self.settings["retainer_slots"]
        over_gil = bool(gil) and t["spend"] > gil
        over_slots = bool(slots) and t["slots"] > slots
        cells = [
            ("LINES", f"{t['lines']:,}",
             f"{len(store.by_stop(self.basket))} worlds to visit"),
            ("CAPITAL NEEDED", f"{t['spend']:,}",
             "more than you have" if over_gil else
             (f"you have {gil:,}" if gil else "set your gil to track this")),
            ("EXPECTED RETURN", f"{t['gain']:,}",
             f"{round(t['gain'] / t['spend'] * 100)}% on outlay" if t["spend"] else "—"),
            ("RETAINER SLOTS", f"{t['slots']} / {slots}" if slots else str(t["slots"]),
             "over your slot count" if over_slots else "listings this run needs"),
        ]
        for i, (k, v, n) in enumerate(cells):
            cell = ttk.Frame(parent, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            parent.columnconfigure(i, weight=1, uniform="bstrip")
            warn = ((k == "CAPITAL NEEDED" and over_gil)
                    or (k == "RETAINER SLOTS" and over_slots))
            ttk.Label(cell, text=k, style="Key.TLabel").pack(anchor="w", padx=14,
                                                             pady=(12, 2))
            ttk.Label(cell, text=v,
                      style="ValWarn.TLabel" if warn else "Val.TLabel").pack(
                          anchor="w", padx=14)
            ttk.Label(cell, text=n, style="Note.TLabel", wraplength=280).pack(
                anchor="w", padx=14, pady=(2, 12))

    def remove_selected(self):
        tree = self.basket_tree
        picked = set()
        for iid in tree.selection():
            vals = tree.item(iid, "values")
            if vals and not tree.get_children(iid):
                picked.add((vals[0], vals[8]))
        if not picked:
            return
        for k, r in list(self.basket.items()):
            if not isinstance(r, dict):
                continue
            if (r.get("name"), r.get("world")) in picked:
                del self.basket[k]
        store.save_basket(self.basket)
        self.fill()

    def clear_basket(self):
        if not self.basket:
            return
        if messagebox.askyesno("Clear list", f"Remove all {len(self.basket)} lines?"):
            self.basket.clear()
            store.save_basket(self.basket)
            self.fill()

    def copy_basket(self):
        if not self.basket:
            return
        lines = []
        for stop, rows in store.by_stop(self.basket).items():
            sub = store.totals(self.basket, rows)
            lines.append(f"== Buy on {stop} -- {sub['spend']:,} gil ==")
            for r in rows:
                lines.append(f"  {r['qty']:>5} x {r['name']} ({r['q']})"
                             f"  @ {r['buy']:,}  ->  sell {r['world']} "
                             f"{r['sell']:,}  (+{r['profit'] * r['qty']:,})")
            lines.append("")
        t = store.totals(self.basket)
        lines.append(f"Total: {t['spend']:,} gil out, {t['gain']:,} back, "
                     f"{t['slots']} retainer slots")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self.status.set(f"Copied {t['lines']} lines to the clipboard.")

    # -- updates -----------------------------------------------------------

    def check_update(self, quiet=False):
        """Ask GitHub whether there's a newer version.

        `quiet` is the startup check: it says nothing when you're up to date
        and says nothing when it fails, because being offline or having no
        repository configured is not something to interrupt anyone about.
        """
        if not quiet:
            self.status.set("Checking GitHub for an update…")

        def work():
            try:
                self.queue.put(("update", ("ok", update.check(), quiet), None))
            except update.UpdateError as exc:
                self.queue.put(("update", ("fail", str(exc), quiet), None))

        threading.Thread(target=work, daemon=True).start()

    def _update_result(self, payload):
        kind, body, quiet = payload
        if kind == "fail":
            if quiet:
                return          # startup check: stay out of the way
            self.status.set("Update check failed.")
            self.show_banner(body, self.check_update, "Try again")
            return
        if not body["available"]:
            if quiet:
                return
            if not body["known"]:
                update.remember(body["sha"], body["slug"])
            self.status.set(f"Up to date — {paths.VERSION} is the newest "
                        f"{body['slug']} has.")
            return
        note = f" — {body['message']}" if body.get("message") else ""
        version = body.get("version", body.get("short", "?"))
        size = body.get("size") or 0
        weight = f", {size / 1048576:.0f} MB" if size else ""
        self.status.set(f"Update {version} available.")
        self.show_banner(
            f"Version {version} is available from {body['slug']}"
            f" ({body.get('date', '')}{weight}){note}. You have {paths.VERSION}.",
            lambda: self._do_update(body), "Install")

    def _do_update(self, info):
        version = info.get("version", info.get("short", "?"))
        if info.get("mode") == "exe":
            question = (f"Replace this app with version {version} from "
                        f"{info['slug']}?\n\nIt updates in place rather than "
                        f"leaving a second copy behind. Every earlier build "
                        f"stays downloadable from the releases page if you "
                        f"ever want one back. You'll need to restart "
                        f"afterwards.")
        else:
            question = (f"Replace this app's files with {version} from "
                        f"{info['slug']}?\n\nThe current files are copied to the "
                        f"backup folder first. You'll need to restart afterwards.")
        if not messagebox.askyesno("Install update", question):
            return
        try:
            changed, pending = update.install(info)
        except update.UpdateError as exc:
            self.status.set("Update failed.")
            self.show_banner(str(exc), self.check_update, "Try again")
            return
        self.hide_banner()
        if pending:
            # The swap can't happen while we're running, so it's queued for the
            # moment we exit. Offer to do that now rather than leaving someone
            # to wonder why the version didn't change.
            self.status.set("Downloaded — closing the app will finish it.")
            question = "\n\n".join([
                "The new version is downloaded, but this folder won't let the "
                "app replace itself while it is running.",
                "Close now to finish installing? It will reopen on the new "
                "version by itself.",
            ])
            if messagebox.askyesno("Nearly done", question):
                self.on_close()
            return
        self.status.set("Updated — restart to run the new version.")
        messagebox.showinfo(
            "Update installed",
            "Replaced: " + ", ".join(changed) +
            "\n\nClose and reopen the app to run it. Your scans, shopping "
            "list and settings are untouched. The version it replaced is "
            "removed as soon as you close the app.")

    # -- item detail -------------------------------------------------------

    def show_detail(self, tier):
        sel = self.trees[tier].selection()
        rows = self.rows_on_screen.get(tier, [])
        if not sel or int(sel[0]) >= len(rows):
            return
        row = rows[int(sel[0])]
        world = self.world[tier]

        win = tk.Toplevel(self)
        win.title(row["n"] + "  -  " + world)
        win.configure(bg=BG)
        win.geometry(f"{self.px(980)}x{self.px(700)}")
        win.transient(self)

        head = ttk.Frame(win)
        head.pack(fill="x", padx=18, pady=(16, 0))
        ttk.Label(head, text=row["n"], style="Head.TLabel").pack(anchor="w")
        ttk.Label(head, style="Dim.TLabel",
                  text=f"{row['q']} · {row['c'] or 'item'} · stacks to {row['st']:,}"
                       f" · selling on {world}").pack(anchor="w", pady=(2, 0))

        facts = ttk.Frame(win, style="Panel.TFrame")
        facts.pack(fill="x", padx=18, pady=(14, 0))
        cells = (("BUY", f"{row['b']:,} on {row['stop']}"),
                 ("SELL", f"{row['s']:,}"),
                 ("NET / UNIT", f"{row['p']:,}  ({row['m']}%)"),
                 ("SELLS", f"{fmt('rate', row['r'])}/day · {fmt('dur', row['d'])} each"))
        for i, (k, v) in enumerate(cells):
            cell = ttk.Frame(facts, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            facts.columnconfigure(i, weight=1, uniform="facts")
            ttk.Label(cell, text=k, style="Key.TLabel").pack(anchor="w", padx=12,
                                                             pady=(10, 1))
            ttk.Label(cell, text=v, style="Note.TLabel", wraplength=160).pack(
                anchor="w", padx=12, pady=(0, 10))

        # Every world's board is already in the scan, so let them all be seen --
        # a few worlds get tabs, thirty-two get a dropdown.
        picker = ttk.Frame(win)
        picker.pack(fill="x", padx=18, pady=(14, 0))
        ttk.Label(picker, text="MARKET BOARD ON", style="Dim.TLabel").pack(
            side="left", padx=(0, 10))
        board_box = ttk.Frame(picker)
        board_box.pack(side="left")

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=(10, 0))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 9))
        self._wall_title = ttk.Label(left, style="Dim.TLabel")
        self._wall_title.pack(anchor="w")
        wtree = ttk.Treeview(left, columns=("p", "q", "r"), show="headings",
                             height=9)
        for key, label, width, anchor in (("p", "Price", 100, "e"),
                                          ("q", "Qty", 50, "e"),
                                          ("r", "Retainer", 150, "w")):
            wtree.heading(key, text=label)
            wtree.column(key, width=self.px(width), anchor=anchor)
        wtree.tag_configure("odd", background=PANEL_2)
        wtree.tag_configure("picked", foreground=GOLD)
        wtree.pack(fill="both", expand=True, pady=(6, 0))
        wall_note = ttk.Label(left, style="Dim.TLabel", wraplength=self.px(330),
                              justify="left")
        wall_note.pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(9, 0))
        ttk.Label(right, text="WHAT IT ACTUALLY SOLD FOR",
                  style="Dim.TLabel").pack(anchor="w")
        canvas = tk.Canvas(right, height=self.px(150), bg=PANEL,
                           highlightthickness=0)
        canvas.pack(fill="x", pady=(6, 0))
        htree = ttk.Treeview(right, columns=("p", "q", "w"), show="headings",
                             height=6)
        for key, label, width in (("p", "Price", 100), ("q", "Qty", 50),
                                  ("w", "When", 90)):
            htree.heading(key, text=label)
            htree.column(key, width=self.px(width),
                         anchor="e" if key != "w" else "w")
        htree.tag_configure("odd", background=PANEL_2)
        htree.pack(fill="both", expand=True, pady=(6, 0))

        boards = row.get("_all") or {world: row}

        sellable = row.get("_sell") or {}

        def show_board(wname, side="sell"):
            source = (row.get("_buy") or {}) if side == "buy" else boards
            v = source.get(wname) or {}
            wall = v.get("wall") or []
            hist = v.get("hist") or []
            sell = v.get("s", row["s"])

            where = "BUYING FROM" if side == "buy" else "THE WALL ON"
            self._wall_title.configure(text=f"{where} {wname.upper()}")
            wtree.delete(*wtree.get_children())
            yours = store.retainers_on(self.settings.get("retainers"), wname)
            mine_here = 0
            if wall:
                for i, pair in enumerate(wall):
                    who = (pair[2] if len(pair) > 2 else None) or ""
                    is_mine = who.lower() in yours if yours else False
                    mine_here += 1 if is_mine else 0
                    tags = ["odd"] if i % 2 else []
                    if is_mine:
                        tags.append("picked")
                    wtree.insert("", "end", tags=tags,
                                 values=(f"{pair[0]:,}", f"{pair[1]:,}",
                                         (who + "  ← you") if is_mine else who))
                units = sum(pair[1] for pair in wall)
                note = (f"{units:,} units across {len(wall)} listings on {wname}. "
                        f"A tall wall at one price is usually a single seller, "
                        f"not a market.")
                if mine_here:
                    note += (f"  {mine_here} of these "
                             f"{'is' if mine_here == 1 else 'are'} yours.")
            else:
                wtree.insert("", "end",
                             values=("— none listed —", "", ""))
                note = (f"Nothing is listed on {wname} right now, so you would be "
                        f"setting the price. The sale history beside this still "
                        f"shows what it has been going for.")

            if side == "buy":
                note = (f"{v.get('l', 0):,} units listed on {wname}, cheapest "
                        f"{v.get('s', 0):,}. This is the buying side — the sale "
                        f"history opposite is for where you'd sell, not here.")
            else:
                profit = sellable.get(wname)
                if profit:
                    note += (f"  Selling here nets {profit['p']:,} a unit "
                             f"({profit['m']}%).")
                elif v.get("so"):
                    note += ("  Not in the table for this world: it doesn't clear "
                             "the margin once you've paid to buy it.")
            wall_note.configure(text=note)

            htree.delete(*htree.get_children())
            for i, entry in enumerate(hist):
                ago = (time.time() - entry[2]) / 86400
                htree.insert("", "end", tags=("odd",) if i % 2 else (),
                             values=(f"{entry[0]:,}", f"{entry[1]:,}",
                                     fmt("dur", ago) + " ago"))
            if side == "buy":
                keep = (boards.get(world) or {})
                hist = keep.get("hist") or []
                sell = keep.get("s", row["s"])
            canvas.after(30, lambda h=hist, sp=sell:
                         self._draw_history(canvas, h, sp))

        buy_boards = row.get("_buy") or {}
        if buy_boards:
            ttk.Label(picker, text="   BUYING ON", style="Dim.TLabel").pack(
                side="left", padx=(18, 8))
            buy_names = sorted(buy_boards,
                               key=lambda n: buy_boards[n].get("s", 0))
            buy_pick = tk.StringVar(value=buy_names[0])
            buy_combo = ttk.Combobox(picker, textvariable=buy_pick, width=18,
                                     state="readonly", font=self.f_body,
                                     values=[f"{n}  {buy_boards[n]['s']:,}"
                                             for n in buy_names])
            buy_combo.pack(side="left")
            buy_combo.current(0)

            def show_buy(_e=None):
                idx = buy_combo.current()
                if 0 <= idx < len(buy_names):
                    show_board(buy_names[idx], side="buy")

            buy_combo.bind("<<ComboboxSelected>>", show_buy)
            ttk.Label(picker, style="Dim.TLabel",
                      text=f"  {len(buy_names)} worlds selling it").pack(side="left")

        names = sorted(boards)
        if len(names) <= 8:
            buttons = {}

            def choose(wname):
                for n, b in buttons.items():
                    b.configure(style="WorldOn.TButton" if n == wname
                                else "World.TButton")
                show_board(wname)

            for wname in names:
                mark = "" if wname in sellable else " ·"
                b = ttk.Button(board_box, text=wname + mark,
                               command=lambda n=wname: choose(n))
                b.pack(side="left", padx=(0, 6))
                buttons[wname] = b
            ttk.Label(board_box, style="Dim.TLabel",
                      text="  · = not worth selling there").pack(side="left")
            choose(world if world in boards else names[0])
        else:
            pick = tk.StringVar(value=world if world in boards else names[0])
            combo = ttk.Combobox(board_box, textvariable=pick, width=18,
                                 state="readonly", values=names, font=self.f_body)
            combo.pack(side="left")
            combo.bind("<<ComboboxSelected>>", lambda e: show_board(pick.get()))
            ttk.Label(board_box, style="Dim.TLabel",
                      text=f"  {len(names)} worlds priced").pack(side="left")
            show_board(pick.get())

        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=18, pady=14)
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="Open on Universalis",
                   command=lambda: webbrowser.open(
                       "https://universalis.app/market/%d" % row["id"])).pack(
                           side="right", padx=(0, 8))
        ttk.Button(foot, text="I listed this", style="Go.TButton",
                   command=lambda: self._ask_position(row, world, win)).pack(side="left")
        ttk.Button(foot, text="Refresh this item",
                   command=lambda: (win.destroy(),
                                    self.refresh_row(tier, row))).pack(
                                        side="left", padx=(8, 0))
        win.bind("<Escape>", lambda e: win.destroy())

    def _draw_history(self, canvas, hist, sell):
        """Sale prices over time: oldest on the left, newest on the right."""
        canvas.delete("all")
        w = max(canvas.winfo_width(), 320)
        h = 150
        pad_l, pad_r, pad_t, pad_b = 60, 12, 14, 20
        if not hist:
            canvas.create_text(w / 2, h / 2, text="no recorded sales",
                               fill=INK_3, font=self.f_body)
            return
        points = sorted(hist, key=lambda e: e[2])
        prices = [e[0] for e in points]
        lo, hi = min(prices), max(prices)
        if hi == lo:
            lo, hi = lo * 0.95, hi * 1.05
        times = [e[2] for e in points]
        t0, t1 = min(times), max(times)
        span = max(t1 - t0, 1)

        def x_of(t):
            return pad_l + (w - pad_l - pad_r) * (t - t0) / span

        def y_of(price):
            return pad_t + (h - pad_t - pad_b) * (1 - (price - lo) / (hi - lo))

        for frac in (0, 0.5, 1):
            y = pad_t + (h - pad_t - pad_b) * frac
            canvas.create_line(pad_l, y, w - pad_r, y, fill=LINE)
            canvas.create_text(pad_l - 6, y, anchor="e", fill=INK_3,
                               font=(self.f_body[0], 8),
                               text=f"{round(hi - (hi - lo) * frac):,}")
        if lo <= sell <= hi:
            y = y_of(sell)
            canvas.create_line(pad_l, y, w - pad_r, y, fill=GOLD, dash=(3, 3))
            canvas.create_text(w - pad_r, y - 8, anchor="e", fill=GOLD,
                               font=(self.f_body[0], 8), text="your price")
        coords = []
        for e in points:
            coords += [x_of(e[2]), y_of(e[0])]
        if len(coords) >= 4:
            canvas.create_line(*coords, fill=GOOD, width=2)
        for e in points:
            x, y = x_of(e[2]), y_of(e[0])
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=GOOD, outline=PANEL)
        for ts, anchor, x in ((t0, "w", pad_l), (t1, "e", w - pad_r)):
            days = (time.time() - ts) / 86400
            canvas.create_text(x, h - 6, anchor=anchor, fill=INK_3,
                               font=(self.f_body[0], 8),
                               text=fmt("dur", days) + " ago")

    def _ask_position(self, row, world, parent):
        """Record what you actually listed, so later checks can spot undercuts."""
        dlg = tk.Toplevel(parent)
        dlg.title("Record a listing")
        dlg.configure(bg=BG)
        dlg.transient(parent)
        dlg.resizable(False, False)
        ttk.Label(dlg, text=f"{row['n']} ({row['q']}) on {world}").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(16, 2))
        ttk.Label(dlg, style="Dim.TLabel", wraplength=self.px(300), justify="left",
                  text="The game lists each stack separately, so six stacks of 99 "
                       "is six listings at whatever price you set. Record them the "
                       "same way here.").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 10))
        price = tk.StringVar(value=str(row["s"]))
        qty = tk.StringVar(value=str(min(row["u"], row["st"])))
        count = tk.StringVar(value="1")
        fields = (("Price per unit", price),
                  ("Units in each listing", qty),
                  ("How many listings", count))
        for i, (label, var) in enumerate(fields):
            ttk.Label(dlg, text=label, style="Dim.TLabel").grid(
                row=2 + i, column=0, sticky="e", padx=(16, 8), pady=4)
            ttk.Entry(dlg, textvariable=var, width=14, font=self.f_body).grid(
                row=2 + i, column=1, sticky="w", padx=(0, 16), pady=4)

        def save():
            def digits(var, default=0):
                text = "".join(c for c in var.get() if c.isdigit())
                return int(text) if text else default
            unit_price, per, listings = digits(price), digits(qty), digits(count, 1)
            if not unit_price or not per or not listings:
                return
            for _ in range(min(listings, 40)):
                store.open_position(self.positions, row, world, unit_price, per)
            store.save_positions(self.positions)
            self.draw_positions()
            dlg.destroy()
            self.status.set(
                f"Tracking {listings} listing{'' if listings == 1 else 's'} of "
                f"{row['n']} on {world} at {unit_price:,} each.")

        btns = ttk.Frame(dlg)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", padx=16, pady=(10, 16))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="Track it", style="Go.TButton", command=save).pack(
            side="right", padx=(0, 8))
        dlg.bind("<Return>", lambda e: save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # -- my listings -------------------------------------------------------

    def _positions_tab(self, parent):
        ttk.Label(parent, style="Dim.TLabel", wraplength=1320, justify="left",
                  text="Find my listings searches every marketable item on each "
                       "world you have a retainer registered on in Settings, and "
                       "shows where you sit on each board. A world takes under a "
                       "minute. What the tab shows when you open it is a quick peek "
                       "at the last scan, which only covers a few hundred items — "
                       "press the button for the real thing."
                  ).pack(anchor="w", pady=(4, 8))
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 10))
        ttk.Button(top, text="Find my listings", style="Go.TButton",
                   command=self.find_mine).pack(side="left")
        ttk.Button(top, text="Check prices",
                   command=self.check_positions).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Stop tracking selected",
                   command=self.drop_position).pack(side="right")

        cols = (("name", "Item / listing", 250, "w"), ("q", "Q", 34, "center"),
                ("world", "World", 110, "w"), ("price", "Your price", 100, "e"),
                ("qty", "Units", 60, "e"), ("low", "Board low", 100, "e"),
                ("under", "Under you", 80, "e"), ("state", "Position", 200, "w"),
                ("when", "Listed", 90, "w"))
        tree = ttk.Treeview(parent, columns=[c[0] for c in cols],
                            show="tree headings", selectmode="extended")
        tree.column("#0", width=self.px(18), stretch=False)
        for key, label, width, anchor in cols:
            tree.heading(key, text=label)
            tree.column(key, width=self.px(width), anchor=anchor,
                        stretch=(key == "name"))
        tree.tag_configure("odd", background=PANEL_2)
        tree.tag_configure("bad", foreground=WARN)
        tree.tag_configure("good", foreground=GOOD)
        tree.bind("<Double-1>", lambda e: self.show_board_detail())
        tree.bind("<Return>", lambda e: self.show_board_detail())
        tree.pack(fill="both", expand=True)
        self.positions_tree = tree
        self.position_keys = {}
        self.listing_boards = {}

    def draw_positions(self):
        """Group by board, with each stack shown as its own listing under it.

        Two sources feed this: listings discovered from the scan by retainer
        name, and any you recorded by hand. Discovered ones are shown first,
        because they're the ones the app is sure about.
        """
        tree = self.positions_tree
        tree.delete(*tree.get_children())
        self.position_keys = {}
        self.listing_boards = {}

        for board, rows in self._discovered_boards().items():
            name, quality, world = board
            units = sum(r["qty"] or 0 for r in rows)
            best = min((r["position"] for r in rows), default=0)
            under = min((r["under"] for r in rows), default=0)
            owned = rows[0].get("mine_on_board") or len(rows)
            summary = ("cheapest on the board" if best == 1
                       else f"{under:,} units from others under you")
            if owned > 1:
                summary += f" · {owned} of these listings are yours"
            parent = tree.insert(
                "", "end", open=True,
                tags=("group",) if best == 1 else ("group", "bad"),
                values=(f"{name}  ({len(rows)} listings)", quality, world, "",
                        f"{units:,}", "", f"{under:,}", summary, "from the scan"))
            board = {"id": rows[0].get("id"), "name": name, "q": quality,
                     "world": world}
            self.listing_boards[parent] = board
            for i, r in enumerate(rows):
                tags = ["odd"] if i % 2 else []
                tags.append("good" if r["position"] == 1 else "bad")
                others = r.get("of", 1) - 1
                if r["position"] == 1:
                    where = ("cheapest on the board" if not r.get("ties")
                             else f"tied cheapest with {r['ties']} other"
                                  f"{'' if r['ties'] == 1 else 's'}")
                else:
                    where = (f"{r.get('sellers_under', 0)} other seller"
                             f"{'' if r.get('sellers_under') == 1 else 's'} "
                             f"cheaper, of {others}")
                node = tree.insert(parent, "end", tags=tags, values=(
                    f"    {r['retainer']}", "", "", f"{r['price']:,}",
                    f"{r['qty']:,}", "", f"{r['under']:,}", where, ""))
                self.listing_boards[node] = dict(board, price=r["price"],
                                                 retainer=r["retainer"])

        groups = store.positions_by_board(self.positions)
        order = sorted(groups.items(),
                       key=lambda kv: (str(kv[1][0][1].get("name", "")),
                                       str(kv[1][0][1].get("world", ""))))
        for board, rows in order:
            first = rows[0][1]
            units = sum(r[1].get("qty", 0) for r in rows)
            undercut = sum(1 for r in rows if r[1].get("state") == "undercut")
            low = next((r[1].get("low") for r in rows if r[1].get("low")), None)
            summary = (f"{undercut} of {len(rows)} undercut" if undercut
                       else ("all cheapest" if any(r[1].get("state") == "lowest"
                                                   for r in rows)
                             else "not checked yet"))
            parent = tree.insert(
                "", "end", open=True,
                tags=("group",) if not undercut else ("group", "bad"),
                values=(f"{first.get('name', '?')}  ({len(rows)} listings)",
                        first.get("q", ""), first.get("world", "?"), "",
                        f"{units:,}", f"{low:,}" if low else "—", "",
                        summary, ""))
            manual_board = {"id": first.get("id"), "name": first.get("name", "?"),
                            "q": first.get("q", "NQ"),
                            "world": first.get("world", "")}
            self.listing_boards[parent] = manual_board
            for i, (key, pos) in enumerate(rows):
                tags = ["odd"] if i % 2 else []
                if pos.get("state") == "undercut":
                    tags.append("bad")
                elif pos.get("state") == "lowest":
                    tags.append("good")
                listed_at = pos.get("at")
                days = ((time.time() - listed_at / 1000) / 86400
                        if isinstance(listed_at, (int, float)) else None)
                node = tree.insert(parent, "end", tags=tags, values=(
                    "", "", "", f"{pos.get('price', 0):,}",
                    f"{pos.get('qty', 0):,}",
                    f"{pos['low']:,}" if pos.get("low") else "—",
                    f"{pos.get('under', 0):,}",
                    pos.get("note", "not checked yet"),
                    (fmt("dur", days) + " ago") if days is not None else "—"))
                self.position_keys[node] = key
                self.listing_boards[node] = dict(manual_board,
                                                 price=pos.get("price"))

    def _auto_find_mine(self, announce=False):
        """Populate discovered listings silently when we can.

        It reads the cached scan rather than the network, so there's no reason
        to make anyone press a button for it. Stays quiet when there's nothing
        to say; the button gives the full explanation on demand.
        """
        if getattr(self, "discovered", None):
            return
        if not store.clean_retainers(self.settings.get("retainers")):
            return
        try:
            found = engine.find_my_listings(
                store.clean_retainers(self.settings.get("retainers")),
                engine.load_geo()["worlds_all"])
        except Exception:
            return
        if found:
            self.discovered = found
            self.draw_positions()
            if announce:
                lowest = sum(1 for r in found if r["position"] == 1)
                self.status.set(f"Found {len(found)} of your listings in the new "
                                f"scan — {lowest} cheapest on their board.")

    def find_mine(self):
        """Sweep whole worlds for your listings -- the thorough version.

        The cached scan covers ~3.5% of the catalogue, so searching it finds
        almost nothing. This reads every item on your retainers' worlds.
        """
        if self.worker and self.worker.is_alive():
            self.status.set("Already busy — let the current job finish first.")
            return
        retainers = store.clean_retainers(self.settings.get("retainers"))
        if not retainers:
            self.status.set("No retainers set — add them under Settings first.")
            self.show_banner(
                "To find your listings the app needs to know your retainer names. "
                "Settings → My retainers, with the world each one lives on.",
                self.open_settings, "Open Settings")
            return
        worlds = sorted({r["world"] for r in retainers if r.get("world")})
        if not worlds:
            self.status.set("Your retainers have no world set — fix that in "
                            "Settings so the right board gets searched.")
            return

        self.status.set(f"Searching every item on {', '.join(worlds)}…")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))
        self.progress["value"] = 0
        names = self.name_index or {}

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def work():
            try:
                found, done = engine.sweep_for_listings(
                    retainers, names, report, self.stop_flag.is_set)
                self.queue.put(("swept", (found, done, len(retainers)), None))
            except Exception as exc:
                self.queue.put(("lookupfail", str(exc), None))

        self.stop_flag.clear()
        self.job = "sweep"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _swept(self, found, worlds, retainer_count):
        self.discovered = found
        self.draw_positions()
        where = ", ".join(worlds)
        if not found:
            self.status.set(
                f"Searched every item on {where} and found nothing for your "
                f"{retainer_count} retainer"
                f"{'' if retainer_count == 1 else 's'}.")
            self.show_banner(
                f"No listings found on {where}. The search covered every "
                f"marketable item, so the names or worlds are the thing to "
                f"check — they must match the game exactly, and a retainer only "
                f"shows up while it has something on the board.",
                self.open_settings, "Open Settings")
            return
        boards = len({(r["id"], r["q"], r["world"]) for r in found})
        units = sum(r["qty"] or 0 for r in found)
        lowest = sum(1 for r in found if r["position"] == 1)
        self.status.set(
            f"Found {len(found)} listings ({units:,} units) across {boards} "
            f"board{'' if boards == 1 else 's'} on {where} — {lowest} cheapest "
            f"on their board.")

    def _scan_has_retainers(self):
        """Whether the cached scan is new enough to carry retainer names."""
        try:
            import json
            with open(engine.SNAPSHOT, encoding="utf-8-sig") as fh:
                snap = json.load(fh)
            for payload in (snap.get("data") or {}).values():
                for l in (payload.get("listings") or [])[:1]:
                    return "r" in l
        except Exception:
            pass
        return False

    def _discovered_boards(self):
        """Discovered listings grouped by item/quality/world."""
        out = {}
        for r in getattr(self, "discovered", []) or []:
            out.setdefault((r["name"], r["q"], r["world"]), []).append(r)
        for rows in out.values():
            rows.sort(key=lambda r: r["price"] or 0)
        return dict(sorted(out.items()))

    def show_board_detail(self):
        """Open the live board behind a listing row.

        Knowing you have been undercut is half an answer; the useful half is by
        how much, and by whom. This reads the board fresh rather than using the
        scan, because a listing you are watching is exactly the case where
        minutes-old numbers are not good enough.
        """
        sel = self.positions_tree.selection()
        if not sel:
            return
        board = self.listing_boards.get(sel[0])
        if not board or not board.get("id") or not board.get("world"):
            self.status.set("That row has no board attached — it was saved by "
                            "an older version. Press Find my listings again.")
            return
        if self.worker and self.worker.is_alive():
            self.status.set("Already busy — let the current job finish first.")
            return

        self.status.set(f"Reading {board['name']} on {board['world']}…")

        def work():
            try:
                listings, history = engine.read_board(
                    board["id"], board["q"], board["world"])
                self.queue.put(("board", (board, listings, history), None))
            except Exception as exc:
                self.queue.put(("lookupfail", str(exc), None))

        self.stop_flag.clear()
        self.job = "board"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _show_board(self, board, listings, history):
        yours = store.retainers_on(self.settings.get("retainers"), board["world"])
        mine = [l for l in listings
                if str(l.get("retainerName") or "").lower() in yours]
        others = [l for l in listings
                  if str(l.get("retainerName") or "").lower() not in yours]
        my_price = board.get("price") or (mine[0]["pricePerUnit"] if mine else None)
        cheapest_other = others[0]["pricePerUnit"] if others else None

        win = tk.Toplevel(self)
        win.title(f"{board['name']} - {board['world']}")
        win.configure(bg=BG)
        win.geometry(f"{self.px(780)}x{self.px(660)}")
        win.transient(self)

        head = ttk.Frame(win)
        head.pack(fill="x", padx=18, pady=(16, 0))
        ttk.Label(head, text=board["name"], style="Head.TLabel").pack(anchor="w")
        ttk.Label(head, style="Dim.TLabel",
                  text=f"{board['q']} · {board['world']} · read just now"
                  ).pack(anchor="w", pady=(2, 0))

        # The headline: the gap, in gil and percent.
        facts = ttk.Frame(win, style="Panel.TFrame")
        facts.pack(fill="x", padx=18, pady=(14, 0))
        if my_price and cheapest_other is not None and cheapest_other < my_price:
            gap = my_price - cheapest_other
            pct = round(gap / my_price * 100)
            beaten = sum(l["quantity"] for l in others
                         if l["pricePerUnit"] < my_price)
            verdict = ("UNDERCUT BY", f"{gap:,}",
                       f"{pct}% under your {my_price:,} · {beaten:,} units cheaper")
        elif my_price and cheapest_other is not None:
            lead = cheapest_other - my_price
            verdict = ("YOU ARE CHEAPEST", f"{lead:,}",
                       f"clear of the next seller at {cheapest_other:,}")
        elif my_price:
            verdict = ("NO COMPETITION", "—",
                       "nobody else is listing this here")
        else:
            verdict = ("YOUR PRICE", "unknown",
                       "no listing of yours found on this board")
        cells = (verdict,
                 ("CHEAPEST HERE",
                  f"{listings[0]['pricePerUnit']:,}" if listings else "—",
                  f"{len(listings)} listings, "
                  f"{sum(l['quantity'] for l in listings):,} units"),
                 ("YOURS ON THIS BOARD", f"{len(mine)}",
                  f"{sum(l['quantity'] for l in mine):,} units"
                  if mine else "none found"))
        for i, (k, v, note) in enumerate(cells):
            cell = ttk.Frame(facts, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            facts.columnconfigure(i, weight=1, uniform="bfacts")
            ttk.Label(cell, text=k, style="Key.TLabel").pack(anchor="w", padx=12,
                                                             pady=(10, 1))
            style = ("ValWarn.TLabel" if k == "UNDERCUT BY" else "Val.TLabel")
            ttk.Label(cell, text=v, style=style).pack(anchor="w", padx=12)
            ttk.Label(cell, text=note, style="Note.TLabel",
                      wraplength=self.px(210)).pack(anchor="w", padx=12,
                                                    pady=(1, 10))

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 0))

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 9))
        ttk.Label(left, text="EVERY LISTING, CHEAPEST FIRST",
                  style="Dim.TLabel").pack(anchor="w")
        wtree = ttk.Treeview(left, columns=("p", "q", "r"), show="headings",
                             height=12)
        for key, label, width, anchor_ in (("p", "Price", 100, "e"),
                                           ("q", "Qty", 50, "e"),
                                           ("r", "Retainer", 150, "w")):
            wtree.heading(key, text=label)
            wtree.column(key, width=self.px(width), anchor=anchor_)
        wtree.tag_configure("odd", background=PANEL_2)
        wtree.tag_configure("picked", foreground=GOLD)
        wtree.tag_configure("under", foreground=WARN)
        for i, l in enumerate(listings[:40]):
            who = str(l.get("retainerName") or "")
            is_mine = who.lower() in yours
            tags = ["odd"] if i % 2 else []
            if is_mine:
                tags.append("picked")
            elif my_price and l["pricePerUnit"] < my_price:
                tags.append("under")
            wtree.insert("", "end", tags=tags,
                         values=(f"{l['pricePerUnit']:,}", f"{l['quantity']:,}",
                                 (who + "  <- you") if is_mine else who))
        wtree.pack(fill="both", expand=True, pady=(6, 0))
        ttk.Label(left, style="Dim.TLabel", wraplength=self.px(330),
                  justify="left",
                  text="Gold is yours. Orange is priced below you — those are "
                       "the ones that sell first."
                  ).pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(9, 0))
        ttk.Label(right, text="WHAT IT ACTUALLY SOLD FOR",
                  style="Dim.TLabel").pack(anchor="w")
        hist = [[h["pricePerUnit"], h["quantity"], h["timestamp"]]
                for h in sorted(history, key=lambda x: -x["timestamp"])[:14]]
        canvas = tk.Canvas(right, height=self.px(150), bg=PANEL,
                           highlightthickness=0)
        canvas.pack(fill="x", pady=(6, 0))
        canvas.after(30, lambda: self._draw_history(canvas, hist, my_price or 0))
        htree = ttk.Treeview(right, columns=("p", "q", "w"), show="headings",
                             height=8)
        for key, label, width in (("p", "Price", 100), ("q", "Qty", 50),
                                  ("w", "When", 90)):
            htree.heading(key, text=label)
            htree.column(key, width=self.px(width),
                         anchor="e" if key != "w" else "w")
        htree.tag_configure("odd", background=PANEL_2)
        for i, e in enumerate(hist):
            ago = (time.time() - e[2]) / 86400
            htree.insert("", "end", tags=("odd",) if i % 2 else (),
                         values=(f"{e[0]:,}", f"{e[1]:,}",
                                 fmt("dur", ago) + " ago"))
        if not hist:
            htree.insert("", "end", values=("no recorded sales", "", ""))
        htree.pack(fill="both", expand=True, pady=(6, 0))

        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=18, pady=14)
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="Open on Universalis",
                   command=lambda: webbrowser.open(
                       "https://universalis.app/market/%d" % board["id"])).pack(
                           side="right", padx=(0, 8))
        win.bind("<Escape>", lambda e: win.destroy())
        self.status.set(
            f"{board['name']} on {board['world']}: " +
            (f"undercut by {my_price - cheapest_other:,}"
             if my_price and cheapest_other is not None
             and cheapest_other < my_price else "you are the cheapest"))

    def check_positions(self):
        """Re-read every board you have something on: discovered or recorded."""
        discovered = getattr(self, "discovered", []) or []
        if not self.positions and not discovered:
            self.status.set("Nothing to check yet — press Find my listings, or "
                            "record one from an item's detail panel.")
            return
        if self.worker and self.worker.is_alive():
            self.status.set("Already busy — let the current job finish first.")
            return
        self.status.set("Checking your listings…")
        self.progress.pack(fill="x", padx=18, pady=(10, 0))
        self.progress["value"] = 0

        def report(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        retainers = store.clean_retainers(self.settings.get("retainers"))
        boards = sorted({(r["id"], r["q"], r["world"]) for r in discovered})
        names = {r["id"]: r["name"] for r in discovered}

        def work():
            try:
                fresh, checked = ([], set())
                if boards and retainers:
                    fresh, checked = engine.recheck_boards(
                        boards, retainers, report, self.stop_flag.is_set)
                    for r in fresh:
                        r["name"] = names.get(r["id"]) or f"item {r['id']}"
                manual = (engine.check_positions(dict(self.positions), retainers,
                                                 report)
                          if self.positions else {})
                self.queue.put(("positions", (manual, fresh, checked), None))
            except Exception as exc:
                self.queue.put(("error", exc, None))

        self.job = "positions"
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _positions_result(self, payload):
        manual, fresh, checked = payload
        gone = 0
        if checked:
            # Anything on a board we just read that no longer carries one of your
            # retainers has left it -- sold, or you took it down. This is the
            # thing the app genuinely couldn't tell you before.
            before = {(r["id"], r["q"], r["world"], r["price"], r["retainer"])
                      for r in (self.discovered or [])
                      if (r["id"], r["q"], r["world"]) in checked}
            after = {(r["id"], r["q"], r["world"], r["price"], r["retainer"])
                     for r in fresh}
            gone = len(before - after)
            kept = [r for r in (self.discovered or [])
                    if (r["id"], r["q"], r["world"]) not in checked]
            self.discovered = sorted(kept + fresh,
                                     key=lambda r: (r["name"] or "", r["world"],
                                                    r["price"] or 0))
        undercut = 0
        for key, info in manual.items():
            if key not in self.positions:
                continue
            self.positions[key].update({
                "state": info.get("state"), "low": info.get("low"),
                "under": info.get("under", 0), "note": info.get("note", ""),
            })
            if info.get("state") == "undercut":
                undercut += 1
        store.save_positions(self.positions)
        self.draw_positions()
        bits = []
        if checked:
            lowest = sum(1 for r in self.discovered if r["position"] == 1)
            bits.append(f"{len(self.discovered)} of your listings found, "
                        f"{lowest} cheapest on their board")
            if gone:
                bits.append(f"{gone} no longer on the board — sold, or taken down")
        if self.positions:
            total = len(self.positions)
            bits.append(f"{undercut} of {total} recorded listings undercut"
                        if undercut else
                        f"all {total} recorded listings still cheapest")
        self.status.set(" · ".join(bits) if bits else "Nothing to check.")

    def drop_position(self):
        """Stop tracking the selected listings, or a whole board if you pick it."""
        tree = self.positions_tree
        sel = tree.selection()
        if not sel:
            return
        keys = set()
        for iid in sel:
            children = tree.get_children(iid)
            for node in (children or [iid]):
                key = self.position_keys.get(node)
                if key:
                    keys.add(key)
        for key in keys:
            store.close_position(self.positions, key)
        store.save_positions(self.positions)
        self.draw_positions()
        self.status.set(f"Stopped tracking {len(keys)} "
                        f"listing{'' if len(keys) == 1 else 's'}.")

    # -- misc --------------------------------------------------------------

    def open_item(self, tier):
        sel = self.trees[tier].selection()
        if not sel:
            return
        rows = self.rows_on_screen.get(tier, [])
        idx = int(sel[0])
        if idx < len(rows):
            webbrowser.open(f"https://universalis.app/market/{rows[idx]['id']}")

    def on_close(self):
        self.settings["worlds"] = dict(self.world)
        store.save_settings(self.settings)
        store.save_basket(self.basket)
        store.save_positions(self.positions)
        # If an update ran this session, the version it replaced is still on
        # disk because a program can't delete the file it is running from.
        # Hand that off to something that outlives us. Never let it stop the
        # window from closing.
        try:
            update.finish_cleanup()
        except Exception:
            pass
        self.destroy()


def report_fatal(exc_type, value, tb):
    """Make a crash visible.

    A windowed build has no console, so an unhandled exception would otherwise
    be completely silent -- the app simply wouldn't appear. Write it down and
    say so, rather than leaving someone staring at a desktop.
    """
    import traceback
    text = "".join(traceback.format_exception(exc_type, value, tb))
    where = "(couldn't write a log file)"
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        where = os.path.join(paths.DATA_DIR, "error.log")
        with io.open(where, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} · "
                     f"v{paths.VERSION} · "
                     f"{'exe' if paths.FROZEN else 'source'} · "
                     f"Python {sys.version.split()[0]} =====\n{text}")
    except OSError:
        pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "FFXIV Market Routes couldn't start",
            f"{exc_type.__name__}: {value}\n\n"
            f"The full details were written to:\n{where}")
        root.destroy()
    except Exception:
        sys.stderr.write(text)


if __name__ == "__main__":
    sys.excepthook = report_fatal

    # Two copies sharing a data folder both scan, which puts us over the API's
    # 8-connection cap and fills the results with holes. Seen for real: two
    # overlapping scans lost 56 of 916 requests.
    update.tidy_after_restart()

    held, other = paths.claim_single_instance()
    if not held:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "Already running",
            "FFXIV Market Routes is already open%s.\n\n"
            "Running two copies from the same folder makes them fight over the "
            "same connection budget, and both end up with incomplete data.\n\n"
            "Use the window that's already open."
            % (f" (process {other})" if other else ""))
        root.destroy()
        raise SystemExit(0)

    try:
        window = App()
        # Tkinter swallows exceptions raised inside callbacks; route them to the
        # same place so a bug in the UI leaves evidence instead of vanishing.
        window.report_callback_exception = report_fatal
        window.mainloop()
    except SystemExit:
        raise
    except Exception:
        report_fatal(*sys.exc_info())
        raise SystemExit(1)
    finally:
        paths.release_single_instance()
