import ctypes
import glob
import json
import os
import subprocess
import sys
import time
import threading
import zlib
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox
from ctypes import wintypes
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageChops, ImageStat

# Long harvests can exceed Pillow's default 178M-pixel safety cap.
Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, 500_000_000)


def configure_windows_dpi():
    """Make Win32 APIs, screenshots, and mouse coords use physical pixels."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


configure_windows_dpi()

import pyautogui

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

APP_NAME = "ScrollHarvest"
MOUSE_MOVE_PAUSE_PX = 50
DEFAULT_SCROLL_STEPS = 80
DEFAULT_MAX_SHOTS = 400
MIN_REGION_PX = 50
RECENT_HARVEST_LIMIT = 5
STITCH_ALIGN_BLOCK = 96
STITCH_ALIGN_WINDOW = 28
STITCH_ALIGN_MAX_DIFF = 12.0
STITCH_SEAM_WINDOW = 96
STITCH_SEAM_BAND = 6
STITCH_MOVE_SEAM = True
STITCH_JOIN_LOG = False
STITCH_JOIN_IMAGE = False

# Dark forest / charcoal, warm cream, muted gold.
THEME = {
    "bg": "#1a231c",
    "panel": "#243028",
    "panel_alt": "#1e2922",
    "raised": "#334038",
    "fg": "#faf3e4",
    "muted": "#e8dcc4",
    "gold": "#e2c36a",
    "gold_dim": "#c4a45a",
    "start_bg": "#e2c36a",
    "start_fg": "#1a231c",
    "entry_bg": "#121a14",
    "border": "#4a5a4c",
    "ok": "#9dcc9d",
    "warn": "#e0c07a",
    "bad": "#e0b0b0",
    "stop_bg": "#5c3a3a",
}

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _win_metrics(dpi_aware):
    user32 = ctypes.windll.user32
    old = None
    try:
        if dpi_aware:
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        pw = user32.GetSystemMetrics(SM_CXSCREEN)
        ph = user32.GetSystemMetrics(SM_CYSCREEN)
        return x, y, w, h, pw, ph
    finally:
        if old:
            try:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


def tk_virtual_screen(widget):
    """Virtual desktop in Tk coordinates so the overlay matches the pointer."""
    if sys.platform == "win32":
        try:
            x, y, w, h, _, _ = _win_metrics(dpi_aware=True)
            sx, sy = tk_to_physical_scale(widget)
            if w > 0 and h > 0 and sx > 0 and sy > 0:
                return (
                    int(round(x / sx)),
                    int(round(y / sy)),
                    int(round(w / sx)),
                    int(round(h / sy)),
                )
        except Exception:
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def tk_to_physical_scale(widget):
    """Ratio of screenshot/mouse pixels to Tk screen coordinates."""
    tk_w = max(widget.winfo_screenwidth(), 1)
    tk_h = max(widget.winfo_screenheight(), 1)
    if sys.platform != "win32":
        return 1.0, 1.0
    try:
        _, _, _, _, phys_w, phys_h = _win_metrics(dpi_aware=True)
        if phys_w > 0 and phys_h > 0:
            return phys_w / tk_w, phys_h / tk_h
    except Exception:
        pass
    return 1.0, 1.0


def to_physical_bbox(region, widget):
    left, top, right, bottom = region
    sx, sy = tk_to_physical_scale(widget)
    return (
        int(round(left * sx)),
        int(round(top * sy)),
        int(round(right * sx)),
        int(round(bottom * sy)),
    )


def normalize_region(raw, widget):
    """Turn saved [l, t, r, b] into a box that still fits this screen, or None."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        left, top, right, bottom = (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
    except (TypeError, ValueError):
        return None
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    vx, vy, vw, vh = tk_virtual_screen(widget)
    left = max(left, vx)
    top = max(top, vy)
    right = min(right, vx + vw)
    bottom = min(bottom, vy + vh)
    if right - left < MIN_REGION_PX or bottom - top < MIN_REGION_PX:
        return None
    return (left, top, right, bottom)


def describe_region(region, saved=False):
    left, top, right, bottom = region
    prefix = "Last area" if saved else "Selected"
    return f"{prefix}: {right - left} × {bottom - top} px at ({left}, {top})"


SW_HIDE = 0
SW_SHOW = 5
GA_ROOT = 2
WM_CLOSE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSENDCHANGING = 0x0400
WDA_EXCLUDEFROMCAPTURE = 0x00000011
OFFSCREEN_POS = -32000

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def toplevel_hwnd(widget):
    user32 = ctypes.windll.user32
    hwnd = int(widget.winfo_id())
    ancestor = user32.GetAncestor(hwnd, GA_ROOT)
    if ancestor:
        return int(ancestor)
    while True:
        parent = user32.GetParent(hwnd)
        if not parent:
            return int(hwnd)
        hwnd = int(parent)


def tk_root_hwnd(widget):
    return toplevel_hwnd(widget)


def hwnd_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value or ""


def list_scrollharvest_hwnds():
    """Every visible top-level window whose title is this app, including leftover dialogs."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length < 1:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        if title == APP_NAME or title.startswith("ScrollHarvest"):
            cls = hwnd_class_name(hwnd)
            # File Explorer uses the save-folder name as its title, so skip it.
            if cls in ("CabinetWClass", "ExploreWClass"):
                return True
            found.append(int(hwnd))
        return True

    cb = WNDENUMPROC(_cb)
    user32.EnumWindows(cb, 0)
    return found


def open_in_explorer(path):
    """Open File Explorer at a folder, or highlight a file in its folder.

    Use explorer.exe switches (not ShellExecute "open") so a Desktop shortcut
    named ScrollHarvest.lnk is never launched instead of the save folder.
    /n forces a new window so an already-open Explorer is not reused.
    """
    target = os.path.abspath(os.path.normpath(path))
    if sys.platform == "win32":
        SW_SHOWNORMAL = 1
        if os.path.isfile(target):
            params = f'/n,/select,"{target}"'
        else:
            os.makedirs(target, exist_ok=True)
            params = f'/n,"{target}"'
        ctypes.windll.shell32.ShellExecuteW(
            None, "open", "explorer.exe", params, None, SW_SHOWNORMAL
        )
        return
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", folder])
        return
    subprocess.Popen(["xdg-open", folder])


def open_file(path):
    """Open a file with the Windows default app. Never used on the save folder."""
    target = os.path.abspath(os.path.normpath(path))
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    if sys.platform == "win32":
        ctypes.windll.shell32.ShellExecuteW(
            None, "open", target, None, None, 1
        )
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
        return
    subprocess.Popen(["xdg-open", target])


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def is_harvest_folder(path, save_root):
    """Only dated harvest folders directly inside the save folder may be trashed."""
    if not path or not save_root:
        return False
    target = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(save_root))
    if not os.path.isdir(target) or os.path.dirname(target) != root:
        return False
    parts = os.path.basename(target).rsplit("_", 2)
    return (
        len(parts) == 3
        and parts[1].isdigit() and len(parts[1]) == 8
        and parts[2].isdigit() and len(parts[2]) == 6
    )


def send_to_recycle_bin(path):
    """Move a file or folder to the Windows Recycle Bin so it can be restored."""
    target = os.path.abspath(os.path.normpath(path))
    if not os.path.exists(target):
        raise FileNotFoundError(target)
    if sys.platform != "win32":
        raise OSError("Recycle Bin is only supported on Windows.")
    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = target + "\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Windows could not move it to the Recycle Bin (code {result}).")
    if os.path.exists(target):
        raise OSError("The folder is still there — a file inside may be open in another app.")


def set_hwnds_visible(hwnds, visible):
    user32 = ctypes.windll.user32
    cmd = SW_SHOW if visible else SW_HIDE
    for hwnd in hwnds:
        user32.ShowWindow(hwnd, cmd)


def close_stray_scrollharvest_windows(keep_hwnd=None):
    """Dismiss leftover 'too small' / info popups so they cannot sit on the page."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    keep = int(keep_hwnd) if keep_hwnd else None
    for hwnd in list_scrollharvest_hwnds():
        if keep is not None and hwnd == keep:
            continue
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


class RegionSelector(tk.Toplevel):
    EDGE = 14
    CURSORS = {
        "n": "sb_v_double_arrow",
        "s": "sb_v_double_arrow",
        "e": "sb_h_double_arrow",
        "w": "sb_h_double_arrow",
        "ne": "size_ne_sw",
        "sw": "size_ne_sw",
        "nw": "size_nw_se",
        "se": "size_nw_se",
        "move": "fleur",
        "new": "cross",
    }

    def __init__(self, master, callback, last_region=None):
        super().__init__(master)
        self.callback = callback
        self._finished = False
        self.last_region = last_region
        self.mode = None
        self.start_x = self.start_y = 0
        self.start_box = None

        vx, vy, vw, vh = tk_virtual_screen(master)
        self.vx, self.vy, self.vw, self.vh = vx, vy, vw, vh
        self.withdraw()
        self.overrideredirect(True)
        self.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.attributes("-alpha", 0.28)
        self.attributes("-topmost", True)
        self.configure(bg="black")
        self.deiconify()
        self.lift()

        self.canvas = tk.Canvas(self, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.rect = None
        self.box = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_hover)
        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Return>", lambda e: self.keep_last())
        self.bind("<KP_Enter>", lambda e: self.keep_last())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        if last_region:
            l, t, r, b = last_region
            self.box = [l - vx, t - vy, r - vx, b - vy]
            self.rect = self.canvas.create_rectangle(
                *self.box, outline="#ffd54a", width=3
            )
            hint = (
                "Gold box is last time. Drag the left or right edge to trim width "
                "without losing the text. Drag inside to move. Drag outside for a new box. "
                "Enter keeps it. Esc cancels"
            )
        else:
            hint = "Drag around the area you want ScrollHarvest to capture • Esc cancels"
        self.hint = tk.Label(
            self,
            text=hint,
            bg="black",
            fg="white",
            font=("Segoe UI", 16, "bold"),
            padx=16,
            pady=10
        )
        self.hint.place(relx=0.5, y=24, anchor="n")

        self.after(50, self._focus)

    def _focus(self):
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass

    def _norm_box(self, x1, y1, x2, y2):
        return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]

    def _hit(self, x, y):
        if not self.box:
            return "new"
        x1, y1, x2, y2 = self.box
        edge = self.EDGE
        if x < x1 - edge or x > x2 + edge or y < y1 - edge or y > y2 + edge:
            return "new"
        near_w = abs(x - x1) <= edge
        near_e = abs(x - x2) <= edge
        near_n = abs(y - y1) <= edge
        near_s = abs(y - y2) <= edge
        if near_n and near_w:
            return "nw"
        if near_n and near_e:
            return "ne"
        if near_s and near_w:
            return "sw"
        if near_s and near_e:
            return "se"
        if near_w:
            return "w"
        if near_e:
            return "e"
        if near_n:
            return "n"
        if near_s:
            return "s"
        if x1 <= x <= x2 and y1 <= y <= y2:
            return "move"
        return "new"

    def _redraw(self, outline="#ffd54a"):
        if self.box is None:
            return
        x1, y1, x2, y2 = self._norm_box(*self.box)
        self.box = [x1, y1, x2, y2]
        if self.rect:
            self.canvas.coords(self.rect, x1, y1, x2, y2)
            self.canvas.itemconfig(self.rect, outline=outline, width=3)
        else:
            self.rect = self.canvas.create_rectangle(x1, y1, x2, y2, outline=outline, width=3)

    def _region_from_box(self):
        x1, y1, x2, y2 = self._norm_box(*self.box)
        return (
            int(round(x1 + self.vx)),
            int(round(y1 + self.vy)),
            int(round(x2 + self.vx)),
            int(round(y2 + self.vy)),
        )

    def on_hover(self, event):
        if self.mode:
            return
        hit = self._hit(event.x, event.y)
        try:
            self.canvas.config(cursor=self.CURSORS.get(hit, "cross"))
        except tk.TclError:
            self.canvas.config(cursor="cross")

    def on_press(self, event):
        self.mode = self._hit(event.x, event.y)
        self.start_x, self.start_y = event.x, event.y
        self.start_box = list(self.box) if self.box else None
        if self.mode == "new":
            self.hint.config(
                text="Drag around the area you want ScrollHarvest to capture • Esc cancels",
                fg="white",
            )
            self.box = [event.x, event.y, event.x, event.y]
            if self.rect:
                self.canvas.delete(self.rect)
                self.rect = None
            self._redraw(outline="white")
        else:
            self.hint.config(
                text="Release to set the capture area. Esc cancels.",
                fg="white",
            )

    def on_drag(self, event):
        if not self.mode:
            return
        x = min(max(event.x, 0), self.vw)
        y = min(max(event.y, 0), self.vh)
        if self.mode == "new":
            self.box = [self.start_x, self.start_y, x, y]
        elif self.start_box:
            x1, y1, x2, y2 = self.start_box
            dx, dy = x - self.start_x, y - self.start_y
            if self.mode == "move":
                x1, x2 = x1 + dx, x2 + dx
                y1, y2 = y1 + dy, y2 + dy
            else:
                if "w" in self.mode:
                    x1 = x
                if "e" in self.mode:
                    x2 = x
                if "n" in self.mode:
                    y1 = y
                if "s" in self.mode:
                    y2 = y
            self.box = [x1, y1, x2, y2]
        self._redraw(outline="white" if self.mode == "new" else "#ffd54a")

    def on_release(self, event):
        self.mode = None
        if not self.box:
            return
        self.box = self._norm_box(*self.box)
        left, top, right, bottom = self._region_from_box()
        if right - left < MIN_REGION_PX or bottom - top < MIN_REGION_PX:
            self.hint.config(
                text="Too small — drag a larger rectangle, or drag an edge of the gold box. Esc cancels.",
                fg="#ffd54a",
            )
            if self.start_box:
                self.box = list(self.start_box)
                self._redraw()
            elif self.rect:
                self.canvas.itemconfig(self.rect, outline="#ffd54a")
            return
        self._complete((left, top, right, bottom))

    def keep_last(self):
        if self.box:
            region = self._region_from_box()
            left, top, right, bottom = region
            if right - left >= MIN_REGION_PX and bottom - top >= MIN_REGION_PX:
                self._complete(region)
                return
        if self.last_region:
            self._complete(self.last_region)

    def cancel(self):
        self._complete(None)

    def _complete(self, region):
        if self._finished:
            return
        self._finished = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.callback(region)


try:
    RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE = Image.BILINEAR


def capture_paths(folder):
    return sorted(glob.glob(os.path.join(folder, "capture_*.png")))


def open_rgb(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def cleanup_capture_frames(folder):
    """Delete capture_####.png after a finished long image is on disk."""
    png_path, _stem = existing_long_image(folder)
    if not png_path or os.path.getsize(png_path) < 100:
        return 0
    removed = 0
    for path in capture_paths(folder):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def cleanup_note(removed):
    if removed < 1:
        return ""
    return f" Removed {removed} capture frame{'s' if removed != 1 else ''}."


def row_signature(img):
    """Cheap per-row fingerprint for finding how far the page scrolled."""
    gray = img.convert("L")
    width, height = gray.size
    slim = gray.resize((48, height), RESAMPLE)
    data = slim.tobytes()
    return [sum(data[y * 48:(y + 1) * 48]) / 48.0 for y in range(height)]


def content_region(img):
    width, height = img.size
    left = int(width * 0.04)
    right = int(width * 0.96)
    top = int(height * 0.10)
    bottom = int(height * 0.92)
    if bottom - top < 40 or right - left < 40:
        return img
    return img.crop((left, top, right, bottom))


def align_frames(prev, curr, direction, allow_bridge=False):
    """Return (shift, pixel_score, coarse_shift). shift is 0 if the pair should be skipped."""
    if allow_bridge:
        w_a, h_a = prev.size
        w_b, h_b = curr.size
        a = row_signature(prev.crop((int(w_a * 0.06), 0, int(w_a * 0.94), h_a)))
        b = row_signature(curr.crop((int(w_b * 0.06), 0, int(w_b * 0.94), h_b)))
        height = min(len(a), len(b))
        if height < 60:
            return 0, 1e9, 0
        pad = int(height * 0.08)
        min_span = max(int(height * 0.08), 28)
        best_mse = None
        coarse = 0
        for shift in range(2, height - 2 * pad - min_span + 1):
            span_h = height - 2 * pad - shift
            if span_h < min_span:
                continue
            total = 0.0
            if direction == "up":
                for i in range(pad, pad + span_h):
                    d = a[i] - b[i + shift]
                    total += d * d
            else:
                for i in range(pad, pad + span_h):
                    d = a[i + shift] - b[i]
                    total += d * d
            mse = total / span_h
            if best_mse is None or mse < best_mse:
                best_mse = mse
                coarse = shift
        if coarse <= 1:
            return 0, 1e9, coarse
        shift, score = refine_shift(prev, curr, direction, coarse)
        max_shift = max(int(prev.size[1] * 0.82), 8)
        if shift <= 1 or shift > max_shift or score > 6.0:
            return 0, score, coarse
        return shift, score, coarse

    a = row_signature(content_region(prev))
    b = row_signature(content_region(curr))
    height = min(len(a), len(b))
    if height < 40:
        return 0, 1e9, 0
    min_overlap = max(int(height * 0.18), 30)
    best_mse = None
    coarse = 0

    def mse_for_shift(shift):
        overlap = height - shift
        if overlap < min_overlap:
            return None
        total = 0.0
        if direction == "up":
            for i in range(overlap):
                delta = a[i] - b[i + shift]
                total += delta * delta
        else:
            for i in range(overlap):
                delta = a[i + shift] - b[i]
                total += delta * delta
        return total / overlap

    for shift in range(0, height - min_overlap + 1):
        mse = mse_for_shift(shift)
        if mse is None:
            continue
        if best_mse is None or mse < best_mse:
            best_mse = mse
            coarse = shift

    if coarse <= 1:
        return 0, 1e9, coarse
    shift, score = refine_shift(prev, curr, direction, coarse)
    max_shift = max(int(prev.size[1] * 0.78), 8)
    if shift <= 1 or shift > max_shift or score > STITCH_ALIGN_MAX_DIFF:
        return 0, score, coarse
    return shift, score, coarse


def find_scroll_shift(prev, curr, direction):
    """Return how many pixels of new content curr adds, or 0 if it looks like a duplicate."""
    shift, _score, _coarse = align_frames(prev, curr, direction)
    return shift


def crop_score(img_a, img_b):
    if img_a.size != img_b.size or img_a.size[0] < 8 or img_a.size[1] < 4:
        return 1e9
    diff = ImageChops.difference(img_a.convert("RGB"), img_b.convert("RGB"))
    stat = ImageStat.Stat(diff)
    return sum(stat.mean) / max(len(stat.mean), 1)


def _row_diff(img_a, img_b, y_a, y_b, left, right):
    row_a = img_a.crop((left, y_a, right, y_a + 1))
    row_b = img_b.crop((left, y_b, right, y_b + 1))
    stat = ImageStat.Stat(ImageChops.difference(row_a, row_b))
    return sum(stat.mean) / max(len(stat.mean), 1)


def sticky_bottom_px(prev, curr, max_px=16):
    """Tiny glued-on UI at the bottom of the capture box, identical in both frames."""
    if prev.size != curr.size:
        return 0
    width, height = prev.size
    limit = min(max_px, max(4, height // 10))
    left = int(width * 0.06)
    right = int(width * 0.94)
    if right - left < 16 or height < 40:
        return 0
    # 1px anti-alias / shadow under a status bar should not hide the bar.
    start = 0
    while start < 2 and start < limit:
        y = height - 1 - start
        if _row_diff(prev, curr, y, y, left, right) <= 2.0:
            break
        start += 1
    matched = []
    for i in range(start, limit):
        y = height - 1 - i
        if _row_diff(prev, curr, y, y, left, right) > 2.0:
            break
        matched.append(y)
    if len(matched) < 2:
        return 0
    body_rows = []
    best_body = None
    for body_y in matched:
        kept = [
            y for y in matched
            if _row_diff(prev, prev, y, body_y, left, right) <= 8.0
        ]
        if len(kept) > len(body_rows):
            body_rows = kept
            best_body = body_y
    if best_body is None or len(body_rows) < 2:
        return 0
    y_lo = min(body_rows)
    if y_lo - 1 in matched:
        y_lo -= 1
    total = height - y_lo
    if total < 2 or total > limit:
        return 0
    # Dark art can make a single mid-row look identical; require two probes.
    y_a = int(height * 0.35)
    y_b = int(height * 0.65)
    if (
        _row_diff(prev, curr, y_a, y_a, left, right) < 2.0
        and _row_diff(prev, curr, y_b, y_b, left, right) < 2.0
    ):
        return 0
    # Page padding is the same color as the row above; a status bar is not.
    above = height - 1 - total
    if above >= 0 and _row_diff(prev, prev, height - total, above, left, right) < 3.0:
        return 0
    return total


def _row_var(img, y, left, right):
    row = img.crop((left, y, right, y + 1)).convert("L")
    stat = ImageStat.Stat(row)
    return stat.var[0] if stat.var else 0.0


def _row_mean(img, y, left, right):
    row = img.crop((left, y, right, y + 1)).convert("L")
    stat = ImageStat.Stat(row)
    return stat.mean[0] if stat.mean else 0.0


def trailing_chrome_rows(img, max_px=16):
    """Gray window-chrome at the bottom of a piece sitting on darker art."""
    width, height = img.size
    left = int(width * 0.10)
    right = int(width * 0.90)
    if right - left < 16 or height < 40:
        return 0
    limit = min(max_px, max(4, height // 10))
    body = 0
    ref_y = height - 1
    if _row_var(img, ref_y, left, right) > 80:
        return 0
    for i in range(limit):
        y = height - 1 - i
        if _row_var(img, y, left, right) > 80:
            break
        if _row_diff(img, img, y, ref_y, left, right) > 8.0:
            break
        body = i + 1
        ref_y = y
    if body < 4:
        return 0
    count = body
    for i in range(body, min(body + 2, limit)):
        y = height - 1 - i
        if y < 0 or _row_var(img, y, left, right) > 80:
            break
        count = i + 1
    edge_y = height - count
    above = edge_y - 1
    if above < 0:
        return 0
    body_mean = _row_mean(img, height - 1 - body // 2, left, right)
    above_mean = _row_mean(img, above, left, right)
    # Godot/editor chrome is mid-gray; the art under it is darker. Text pages
    # and paragraph gaps are the same gray as the bar, so they stay put.
    if not (40.0 <= body_mean <= 72.0):
        return 0
    if above_mean > body_mean - 8.0:
        return 0
    return count


_LUT_GT8 = [1 if i > 8 else 0 for i in range(256)]
_LUT_GT10 = [1 if i > 10 else 0 for i in range(256)]
_LUT_GT14 = [1 if i > 14 else 0 for i in range(256)]
_LUT_GT24 = [1 if i > 24 else 0 for i in range(256)]


def overlap_block_score(prev, curr, direction, shift, y_ref=None):
    """Pixel difference of a tall interior overlap block at this vertical offset."""
    if prev.size != curr.size or shift <= 1:
        return 1e9
    width, height = prev.size
    left = int(width * 0.12)
    right = int(width * 0.88)
    if right - left < 24:
        return 1e9
    overlap = height - shift
    if overlap < 32:
        return 1e9
    block = min(STITCH_ALIGN_BLOCK, max(40, overlap // 2))
    margin = min(16, max(4, (overlap - block) // 4))
    max_ref = max(margin, overlap - block - margin)
    if y_ref is None:
        ref = (overlap - block) // 2
    else:
        ref = min(max(int(y_ref), margin), max_ref)
    if direction == "up":
        y_prev = ref
        y_curr = y_prev + shift
    else:
        y_curr = ref
        y_prev = y_curr + shift
    if y_prev < 0 or y_curr < 0 or y_prev + block > height or y_curr + block > height:
        return 1e9
    return crop_score(
        prev.crop((left, y_prev, right, y_prev + block)),
        curr.crop((left, y_curr, right, y_curr + block)),
    )


def _pick_overlap_anchor(prev, curr, direction, coarse_shift):
    """Choose a detail-rich interior band away from top/bottom sticky UI."""
    width, height = prev.size
    overlap = height - coarse_shift
    if overlap < 40:
        return None
    block = min(STITCH_ALIGN_BLOCK, max(40, overlap // 2))
    free = overlap - block
    if free <= 16:
        return max(0, free // 2)
    margin = min(max(int(overlap * 0.16), 8), free // 2)
    mid = free // 2
    lo = margin
    hi = max(margin, free - margin)
    candidates = sorted({mid, (lo + mid) // 2, (mid + hi) // 2})
    left = int(width * 0.12)
    right = int(width * 0.88)
    ref_img = prev if direction == "up" else curr
    best_y = mid
    best_var = -1.0
    for y0 in candidates:
        stat = ImageStat.Stat(ref_img.crop((left, y0, right, y0 + block)).convert("L"))
        var = stat.var[0] if stat.var else 0.0
        if var > best_var * 1.15 + 1.0:
            best_var = var
            best_y = y0
    return best_y


def refine_shift(prev, curr, direction, coarse_shift, window=STITCH_ALIGN_WINDOW):
    """Pixel-verify the row-signature offset on a large overlap block."""
    width, height = prev.size
    if curr.size != prev.size:
        return coarse_shift, overlap_block_score(prev, curr, direction, coarse_shift)
    y_ref = _pick_overlap_anchor(prev, curr, direction, coarse_shift)
    best_shift = coarse_shift
    best_score = None
    second = None
    for delta in range(-window, window + 1):
        shift = coarse_shift + delta
        if shift <= 1 or shift >= height - 36:
            continue
        score = overlap_block_score(prev, curr, direction, shift, y_ref=y_ref)
        if best_score is None or score < best_score:
            second = best_score
            best_score = score
            best_shift = shift
        elif second is None or score < second:
            second = score
    if best_score is None:
        return coarse_shift, 1e9
    # Two nearby text rows can look similar; require a clear winner if error is high.
    if (
        second is not None
        and best_score > 4.0
        and second <= best_score * 1.12
        and abs(second - best_score) < 0.8
    ):
        return coarse_shift, max(best_score, STITCH_ALIGN_MAX_DIFF + 0.1)
    return best_shift, best_score


def _is_static_band(img_a, img_b, y0, y1, x_lo, x_hi):
    """True if [y0, y1) at the same screen Y barely changed while the page scrolled."""
    if y1 - y0 < 3 or x_hi - x_lo < 16:
        return False
    diff = ImageChops.difference(
        img_a.crop((x_lo, y0, x_hi, y1)),
        img_b.crop((x_lo, y0, x_hi, y1)),
    ).convert("L")
    stat = ImageStat.Stat(diff)
    if (stat.mean[0] if stat.mean else 99.0) > 1.8:
        return False
    span = (x_hi - x_lo) * (y1 - y0)
    strong = diff.point(_LUT_GT24).tobytes().count(1)
    return strong <= max(6, int(span * 0.004))


def matching_overlap_span(prev, curr, direction, shift):
    """Find the clean unobstructed overlap [valid_lo, valid_hi) in prev's Y coords.

    Excludes sticky headers, sticky bottom composers/status bars, hover URL bars,
    and semi-transparent fade masks where prev and curr do not match at shift.
    """
    width, height = prev.size
    ov = height - shift
    base_lo = int(width * 0.03)
    base_hi = int(width * 0.97)
    if ov < 12 or base_hi - base_lo < 24:
        if direction == "up":
            return 0, max(ov, 1), base_lo, base_hi
        return shift, height, base_lo, base_hi

    if direction == "up":
        ov_a = prev.crop((0, 0, width, ov))
        ov_b = curr.crop((0, shift, width, height))
    else:
        ov_a = prev.crop((0, shift, width, height))
        ov_b = curr.crop((0, 0, width, ov))
    diff_l = ImageChops.difference(ov_a, ov_b).convert("L")

    # Trim non-scrolling sidebars/scrollbars on the outer 14% using the interior reference band.
    ref_h = min(32, ov)
    ref_y0 = max(0, (ov - ref_h) // 2)
    ref_band = diff_l.crop((0, ref_y0, width, ref_y0 + ref_h))
    max_edge = int(width * 0.14)
    x_lo = base_lo
    while x_lo < max_edge and ref_band.crop((x_lo, 0, x_lo + 1, ref_h)).getextrema()[1] > 8:
        x_lo += 1
    x_hi = base_hi
    min_right = width - max_edge
    while x_hi > min_right and ref_band.crop((x_hi - 1, 0, x_hi, ref_h)).getextrema()[1] > 8:
        x_hi -= 1
    if x_hi - x_lo < 32:
        x_lo, x_hi = int(width * 0.12), int(width * 0.88)

    span = x_hi - x_lo
    sub = diff_l.crop((x_lo, 0, x_hi, ov))
    raw = sub.tobytes()
    gt10 = sub.point(_LUT_GT10).tobytes()
    gt24 = sub.point(_LUT_GT24).tobytes()
    max_changed = max(8, int(span * 0.006))
    max_sum = span * 1.2

    clean = []
    for r in range(ov):
        i0 = r * span
        i1 = i0 + span
        if (
            gt24[i0:i1].count(1) < 3
            and gt10[i0:i1].count(1) < max_changed
            and sum(raw[i0:i1]) <= max_sum
        ):
            clean.append(True)
        else:
            clean.append(False)

    runs = []
    r = 0
    while r < ov:
        if not clean[r]:
            r += 1
            continue
        r0 = r
        while r < ov and clean[r]:
            r += 1
        runs.append((r0, r))

    min_run = min(24, max(6, ov // 4))
    good = [rn for rn in runs if rn[1] - rn[0] >= min_run]
    if not good:
        if runs:
            best = max(runs, key=lambda rn: rn[1] - rn[0])
        else:
            best = (0, ov)
    else:
        substantial = min(36, max(8, ov // 3))
        strong_runs = [rn for rn in good if rn[1] - rn[0] >= substantial]
        pool = strong_runs if strong_runs else good
        if direction == "up":
            # Prefer the clean run reaching closest to the top of prev.
            best = min(pool, key=lambda rn: rn[0])
        else:
            # Prefer the clean run reaching closest to the bottom of prev.
            best = max(pool, key=lambda rn: rn[1])

    offset = 0 if direction == "up" else shift
    return best[0] + offset, best[1] + offset, x_lo, x_hi


def find_quiet_band_center(
    img,
    low,
    high,
    prefer_y,
    band_rows=STITCH_SEAM_BAND,
    x_lo=None,
    x_hi=None,
    other_img=None,
    other_offset=0,
    return_score=False,
):
    """Pick the center of a contiguous low-content horizontal band in [low, high].

    Never relies on a single low-variance row: requires `band_rows` consecutive
    rows with minimal vertical transitions, minimal glyph horizontal edges, and
    low foreground occupancy relative to the dominant background color.
    """
    width, height = img.size
    low = max(1, int(low))
    high = min(height - 1, int(high))
    clamped_pref = max(low, min(high, int(prefer_y)))
    if high - low < band_rows + 2:
        return (clamped_pref, 1e9) if return_score else clamped_pref
    if x_lo is None:
        x_lo = int(width * 0.04)
    if x_hi is None:
        x_hi = int(width * 0.96)
    span = x_hi - x_lo
    if span < 16:
        return (clamped_pref, 1e9) if return_score else clamped_pref

    crop_top = max(0, low - 1)
    crop_bot = min(height, high + 1)
    n_rows = crop_bot - crop_top
    if n_rows < band_rows + 2:
        return (clamped_pref, 1e9) if return_score else clamped_pref

    gray = img.crop((x_lo, crop_top, x_hi, crop_bot)).convert("L")
    hist = gray.histogram()
    # Group into 64 bins of width 4 to find dominant background brightness cleanly.
    best_bin = max(range(64), key=lambda b: sum(hist[b * 4:(b + 1) * 4]))
    bg_val = best_bin * 4 + 2
    lut_fg = [1 if abs(i - bg_val) > 18 else 0 for i in range(256)]

    body = gray.crop((0, 1, span, n_rows))
    above = gray.crop((0, 0, span, n_rows - 1))
    v_diff = ImageChops.difference(body, above)
    v_raw = v_diff.tobytes()
    v_act = v_diff.point(_LUT_GT8).tobytes()
    fg_act = body.point(lut_fg).tobytes()

    h_span = span - 2
    h_diff = ImageChops.difference(
        body.crop((2, 0, span, n_rows - 1)),
        body.crop((0, 0, span - 2, n_rows - 1)),
    )
    h_act = h_diff.point(_LUT_GT14).tobytes()

    cross_raw = None
    if other_img is not None:
        other_top = crop_top + 1 + other_offset
        other_bot = crop_bot + other_offset
        if 0 <= other_top and other_bot <= other_img.size[1]:
            other_body = other_img.crop((x_lo, other_top, x_hi, other_bot)).convert("L")
            cross_raw = ImageChops.difference(body, other_body).tobytes()

    rows_count = n_rows - 1
    row_cost = []
    for i in range(rows_count):
        s0 = i * span
        s1 = s0 + span
        v_cnt = v_act[s0:s1].count(1)
        v_mean = sum(v_raw[s0:s1]) / span
        fg_frac = fg_act[s0:s1].count(1) / span
        hs0 = i * h_span
        h_cnt = h_act[hs0:hs0 + h_span].count(1)
        # Allow up to 6 horizontal transitions for vertical bubble walls / quote bars.
        glyph_h = max(0, h_cnt - 6)
        cost = (v_cnt * 8.0) + (v_mean * 20.0) + (glyph_h * 12.0) + (fg_frac * 4.0)
        if cross_raw is not None:
            c_mean = sum(cross_raw[s0:s1]) / span
            cost += c_mean * 40.0
        row_cost.append(cost)

    if len(row_cost) < band_rows:
        return (clamped_pref, 1e9) if return_score else clamped_pref

    # Prefix sums for fast band + halo queries so cuts center inside wide gaps.
    prefix = [0.0]
    for c in row_cost:
        prefix.append(prefix[-1] + c)

    half = band_rows // 2
    halo_r = max(band_rows + 2, 8)
    best_y = clamped_pref
    best_score = None
    best_raw = 1e9
    for idx in range(0, len(row_cost) - band_rows + 1):
        band = row_cost[idx:idx + band_rows]
        b_max = max(band)
        b_mean = (prefix[idx + band_rows] - prefix[idx]) / band_rows
        h0 = max(0, idx + half - halo_r)
        h1 = min(len(row_cost), idx + half + halo_r)
        halo_mean = (prefix[h1] - prefix[h0]) / max(1, h1 - h0)
        center_y = crop_top + 1 + idx + half
        dist_pen = abs(center_y - prefer_y) * 0.02
        raw_cost = (b_max * 2.0) + b_mean
        score = raw_cost + (halo_mean * 0.05) + dist_pen
        if best_score is None or score < best_score:
            best_score = score
            best_raw = raw_cost
            best_y = center_y
    out_y = max(low, min(high, best_y))
    return (out_y, best_raw) if return_score else out_y


def quiet_seam_lift(img, footer):
    """Compatibility wrapper returning lift into a contiguous quiet band."""
    height = img.size[1]
    high = height - footer
    low = max(8, high - STITCH_SEAM_WINDOW)
    if high - low < STITCH_SEAM_BAND + 2:
        return 0
    seam = find_quiet_band_center(img, low, high, prefer_y=high)
    return max(0, min(high - seam, STITCH_SEAM_WINDOW))


def quiet_row_near(img, target, window=40):
    """Contiguous quiet-band center near target."""
    height = img.size[1]
    return find_quiet_band_center(
        img,
        max(1, target - window),
        min(height - 2, target + window),
        prefer_y=target,
    )


def _append_stitch_debug(folder, info, prev=None, curr=None, shift=0, direction="down"):
    """Record weak joins only — not a file per frame."""
    try:
        with open(os.path.join(folder, "stitch_debug.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(info) + "\n")
    except OSError:
        return
    if info.get("score", 0) < 8 or prev is None or curr is None or shift <= 1:
        return
    try:
        width, height = prev.size
        left = int(width * 0.12)
        right = int(width * 0.88)
        block = min(80, max(24, (height - shift) // 3))
        y_curr = max(int(height * 0.12), 8)
        overlap = height - shift
        if y_curr + block > overlap - 4:
            y_curr = max(4, overlap - block - 4)
        y_prev = y_curr + shift if direction != "up" else max(y_curr - shift, 0)
        a = prev.crop((left, y_prev, right, min(y_prev + block, height)))
        b = curr.crop((left, y_curr, right, min(y_curr + block, height)))
        if a.size[1] != b.size[1]:
            return
        pair = Image.new("RGB", (a.size[0] * 2 + 8, a.size[1]), (20, 20, 20))
        pair.paste(a, (0, 0))
        pair.paste(b, (a.size[0] + 8, 0))
        name = "stitch_debug_%04d.png" % int(info.get("frame", 0))
        pair.save(os.path.join(folder, name))
    except Exception:
        pass


def _log_stitch_join(folder, info, prev, curr):
    """One JSON line per join, plus an optional ±50px seam crop."""
    if not STITCH_JOIN_LOG:
        return
    try:
        with open(os.path.join(folder, "stitch_join.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(info) + "\n")
    except OSError:
        return
    if not STITCH_JOIN_IMAGE or prev is None or curr is None:
        return
    try:
        seam_a = int(info["final_seam_a"])
        seam_b = int(info["final_seam_b"])
        width = min(prev.size[0], curr.size[0])
        pad = 50

        def band(img, seam):
            # Seam is exclusive in A (first not-kept row) and inclusive in B
            # (first kept row). Draw the boundary on the last pixel still in
            # range so a seam at H is visible on A's last row.
            top = max(0, seam - pad)
            bottom = min(img.size[1], max(seam, 0) + pad)
            if bottom <= top:
                return img.crop((0, 0, width, min(img.size[1], pad))).copy()
            crop = img.crop((0, top, width, bottom)).copy()
            y = min(max(seam - top, 0), crop.size[1] - 1)
            draw = crop.load()
            for x in range(crop.size[0]):
                draw[x, y] = (220, 40, 40)
                if y + 1 < crop.size[1]:
                    draw[x, y + 1] = (220, 40, 40)
            return crop

        a_band = band(prev, seam_a)
        b_band = band(curr, seam_b)
        gap = 4
        canvas = Image.new(
            "RGB",
            (width, a_band.size[1] + b_band.size[1] + gap),
            (30, 30, 30),
        )
        canvas.paste(a_band, (0, 0))
        canvas.paste(b_band, (0, a_band.size[1] + gap))
        name = "stitch_join_%04d.png" % int(info.get("frame_b", 0))
        canvas.save(os.path.join(folder, name))
    except Exception:
        pass


def stitch_captures(folder, direction="down"):
    """Build one long image from capture_0001.png, capture_0002.png, ..."""
    paths = capture_paths(folder)
    if len(paths) < 2:
        return None, "Need at least two captures to stitch."

    first = open_rgb(paths[0])
    width, height = first.size
    pieces = []
    prev = first
    prev_idx = 1
    # Telescoping boundary in `prev` frame coordinates:
    #   down: prev emits [prev_bound, seam_a), then next prev_bound = seam_a - shift
    #   up:   prev emits [seam_a, prev_bound), then next prev_bound = seam_a + shift
    prev_bound = 0 if direction != "up" else height
    confirmed_bot_trim = 0
    confirmed_top_trim = 0
    last_aligned_prev = None
    pending_skipped = None
    pending_skipped_idx = 0
    doc_offset = 0
    used = 1
    skipped = 0
    frame_index = 1

    def _emit_join(frame_a_img, frame_a_idx, frame_b_img, frame_b_idx, shift, score, coarse):
        nonlocal prev, prev_idx, prev_bound, confirmed_bot_trim, confirmed_top_trim
        nonlocal last_aligned_prev, doc_offset, used

        h = frame_b_img.size[1]
        shift = min(shift, h - 4)
        valid_lo, valid_hi, x_lo, x_hi = matching_overlap_span(
            frame_a_img, frame_b_img, direction, shift
        )
        max_overlay = min(h // 3, max(0, (h - shift) - 28))

        if direction == "up":
            top_mismatch = valid_lo
            if 3 <= top_mismatch <= max_overlay and _is_static_band(
                frame_a_img, frame_b_img, 0, top_mismatch, x_lo, x_hi
            ):
                confirmed_top_trim = max(confirmed_top_trim, top_mismatch)
            eff_lo = max(valid_lo, confirmed_top_trim)
            if eff_lo > 0 and eff_lo + 4 < min(valid_hi, prev_bound) - 8:
                eff_lo += 4
            eff_hi = min(valid_hi, prev_bound - 2)
            if eff_hi < eff_lo:
                if valid_lo <= prev_bound - 2:
                    eff_lo = valid_lo
                    eff_hi = min(valid_hi, prev_bound - 2)
                if eff_hi < eff_lo:
                    return False
            base_seam_a = eff_lo
            if STITCH_MOVE_SEAM and eff_hi - eff_lo >= STITCH_SEAM_BAND + 2:
                search_hi = min(eff_hi, eff_lo + STITCH_SEAM_WINDOW)
                seam_a = find_quiet_band_center(
                    frame_a_img,
                    eff_lo,
                    search_hi,
                    prefer_y=eff_lo,
                    band_rows=STITCH_SEAM_BAND,
                    x_lo=x_lo,
                    x_hi=x_hi,
                    other_img=frame_b_img,
                    other_offset=shift,
                )
            else:
                seam_a = base_seam_a
            seam_b = seam_a + shift
            if prev_bound - seam_a < 2 or seam_b < 2 or seam_b > h:
                return False
            piece = frame_a_img.crop((0, seam_a, width, prev_bound))
            pieces.insert(0, piece)
            doc_offset -= shift
            _log_stitch_join(
                folder,
                {
                    "frame_a": frame_a_idx,
                    "frame_b": frame_b_idx,
                    "refined_overlap": int(shift),
                    "coarse": int(coarse),
                    "alignment_score": round(float(score), 4),
                    "base_seam_a": int(base_seam_a),
                    "base_seam_b": int(base_seam_a + shift),
                    "safe_seam_delta": int(seam_a - base_seam_a),
                    "final_seam_a": int(seam_a),
                    "final_seam_b": int(seam_b),
                    "pixels_retained_from_a": int(piece.size[1]),
                    "pixels_discarded_from_b": int(h - seam_b),
                    "pixels_appended": int(shift),
                    "document_seam_y": int(doc_offset + seam_b),
                    "footer": int(confirmed_top_trim),
                    "move_seam": bool(STITCH_MOVE_SEAM),
                    "invariant_seam_a_eq_b_plus_shift": bool(seam_b == seam_a + shift),
                    "no_gap": True,
                    "no_dup_strip": True,
                },
                frame_a_img,
                frame_b_img,
            )
            last_aligned_prev = frame_a_img
            prev = frame_b_img
            prev_idx = frame_b_idx
            prev_bound = seam_b
            used += 1
            return True

        # Down-scroll: frame_a[y] == frame_b[y - shift] in the clean overlap.
        bot_mismatch = h - valid_hi
        if 3 <= bot_mismatch <= max_overlay and _is_static_band(
            frame_a_img, frame_b_img, valid_hi, h, x_lo, x_hi
        ):
            confirmed_bot_trim = max(confirmed_bot_trim, bot_mismatch)
        min_seam_a = max(shift, prev_bound + 2)
        eff_hi = min(valid_hi, h - confirmed_bot_trim)
        if eff_hi < min_seam_a:
            eff_hi = valid_hi
        if eff_hi < h and eff_hi - 4 >= min_seam_a + 8:
            eff_hi -= 4
        eff_lo = max(min_seam_a, valid_lo + (4 if valid_lo > shift else 0))
        if eff_lo > eff_hi:
            eff_lo = min_seam_a
        if eff_hi < min_seam_a:
            return False

        base_seam_a = eff_hi
        if STITCH_MOVE_SEAM and eff_hi - eff_lo >= STITCH_SEAM_BAND + 2:
            search_lo = max(eff_lo, eff_hi - STITCH_SEAM_WINDOW)
            seam_a = find_quiet_band_center(
                frame_a_img,
                search_lo,
                eff_hi,
                prefer_y=eff_hi,
                band_rows=STITCH_SEAM_BAND,
                x_lo=x_lo,
                x_hi=x_hi,
                other_img=frame_b_img,
                other_offset=-shift,
            )
        else:
            seam_a = base_seam_a
        seam_b = seam_a - shift
        if seam_a - prev_bound < 2 or seam_b < 0 or seam_b >= h:
            return False

        piece = frame_a_img.crop((0, prev_bound, width, seam_a))
        pieces.append(piece)
        _log_stitch_join(
            folder,
            {
                "frame_a": frame_a_idx,
                "frame_b": frame_b_idx,
                "refined_overlap": int(shift),
                "coarse": int(coarse),
                "alignment_score": round(float(score), 4),
                "base_seam_a": int(base_seam_a),
                "base_seam_b": int(base_seam_a - shift),
                "safe_seam_delta": int(seam_a - base_seam_a),
                "final_seam_a": int(seam_a),
                "final_seam_b": int(seam_b),
                "pixels_retained_from_a": int(piece.size[1]),
                "pixels_discarded_from_b": int(seam_b),
                "pixels_appended": int(shift),
                "document_seam_y": int(doc_offset + seam_a),
                "footer": int(h - eff_hi),
                "move_seam": bool(STITCH_MOVE_SEAM),
                "invariant_seam_a_eq_b_plus_shift": bool(seam_a == seam_b + shift),
                "no_gap": True,
                "no_dup_strip": True,
            },
            frame_a_img,
            frame_b_img,
        )
        doc_offset += shift
        last_aligned_prev = frame_a_img
        prev = frame_b_img
        prev_idx = frame_b_idx
        prev_bound = seam_b
        used += 1
        return True

    for path in paths[1:]:
        frame_index += 1
        curr = open_rgb(path)
        if curr.size != (width, height):
            curr = curr.resize((width, height), RESAMPLE)
        shift, score, coarse = align_frames(prev, curr, direction)
        if shift <= 1 and pending_skipped is not None:
            shift, score, coarse = align_frames(
                prev, curr, direction, allow_bridge=True
            )
        if shift <= 1 and pending_skipped is not None:
            # Direct bridge across the skipped frame failed; step from pending_skipped
            # if pending_skipped -> curr has a verified overlap so later frames aren't lost.
            alt_shift, alt_score, alt_coarse = align_frames(
                pending_skipped, curr, direction
            )
            if alt_shift > 1:
                if direction == "up":
                    if prev_bound > 2:
                        pieces.insert(0, prev.crop((0, 0, width, prev_bound)))
                    prev_bound = height
                else:
                    if height - prev_bound > 2:
                        pieces.append(prev.crop((0, prev_bound, width, height)))
                    prev_bound = 0
                prev = pending_skipped
                prev_idx = pending_skipped_idx
                pending_skipped = None
                pending_skipped_idx = 0
                used += 1
                shift, score, coarse = alt_shift, alt_score, alt_coarse
        if shift <= 1:
            is_dup = crop_score(content_region(prev), content_region(curr)) <= 2.2
            if not is_dup and coarse > 1 and score < 1e8:
                skipped += 1
                pending_skipped = curr
                pending_skipped_idx = frame_index
                _append_stitch_debug(
                    folder,
                    {
                        "frame": frame_index,
                        "coarse": int(coarse),
                        "refined": 0,
                        "score": round(float(score), 4),
                        "seam": None,
                        "skipped": True,
                    },
                    prev,
                    curr,
                    coarse,
                    direction,
                )
            continue

        if _emit_join(prev, prev_idx, curr, frame_index, shift, score, coarse):
            pending_skipped = None
            pending_skipped_idx = 0
        else:
            skipped += 1

    if used < 2 and skipped > 0:
        return None, "Could not find overlapping content to stitch."

    # Emit the final frame's remaining unobstructed content.
    x_check_lo = int(width * 0.08)
    x_check_hi = int(width * 0.92)
    if direction == "up":
        final_top = 0
        if (
            confirmed_top_trim >= 3
            and last_aligned_prev is not None
            and _is_static_band(
                last_aligned_prev, prev, 0, confirmed_top_trim, x_check_lo, x_check_hi
            )
        ):
            final_top = min(confirmed_top_trim, max(0, prev_bound - 2))
        if prev_bound > final_top:
            pieces.insert(0, prev.crop((0, final_top, width, prev_bound)))
    else:
        final_end = height
        if (
            confirmed_bot_trim >= 3
            and last_aligned_prev is not None
            and _is_static_band(
                last_aligned_prev,
                prev,
                height - confirmed_bot_trim,
                height,
                x_check_lo,
                x_check_hi,
            )
        ):
            final_end = max(prev_bound + 2, height - confirmed_bot_trim)
        if final_end > prev_bound:
            pieces.append(prev.crop((0, prev_bound, width, final_end)))

    if not pieces:
        return None, "Could not find overlapping content to stitch."

    total_h = sum(p.size[1] for p in pieces)
    if total_h < 2:
        return None, "Could not find overlapping content to stitch."

    Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, total_h * width + 1)
    canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
    y = 0
    for piece in pieces:
        if piece.size[0] != width:
            piece = piece.resize((width, piece.size[1]), RESAMPLE)
        canvas.paste(piece, (0, y))
        y += piece.size[1]

    huge = total_h * width > 20_000_000
    stem = harvest_file_stem(folder) or "stitched"
    out_path = os.path.join(folder, f"{stem}.png")
    canvas.save(out_path, "PNG", optimize=not huge)
    pdf_note = ""
    try:
        pdf_path = os.path.join(folder, f"{stem}.pdf")
        page_count = export_pdf(canvas, pdf_path)
        pdf_note = f" Saved {stem}.png and {stem}.pdf ({page_count} page{'s' if page_count != 1 else ''})."
    except Exception as exc:
        pdf_note = f" Saved {stem}.png, but PDF failed: {exc}"
    frames_note = cleanup_note(cleanup_capture_frames(folder))
    skip_note = ""
    if skipped:
        skip_note = f" Skipped {skipped} weakly aligned frame{'s' if skipped != 1 else ''}."
        if os.path.isfile(os.path.join(folder, "stitch_debug.jsonl")):
            skip_note += " See stitch_debug.jsonl."
    join_note = ""
    if STITCH_JOIN_LOG and os.path.isfile(os.path.join(folder, "stitch_join.jsonl")):
        join_note = " Join log: stitch_join.jsonl."
        if not STITCH_MOVE_SEAM:
            join_note += " Quiet-row seam movement was off."
    return out_path, (
        f"Stitched {used} frames into one image ({width}×{total_h} px)."
        f"{pdf_note}{frames_note}{skip_note}{join_note}"
    )


def _edge_fill_rgb(img, y_edge, band_px=4):
    """Estimate the page background RGB at a page boundary so vertical margin pads match."""
    width, height = img.size
    y0 = max(0, min(height - 1, y_edge))
    y1 = min(height, y0 + max(1, band_px))
    strip = img.crop((int(width * 0.04), y0, int(width * 0.96), y1)).convert("RGB")
    raw = strip.tobytes()
    n_px = len(raw) // 3
    if n_px < 1:
        return (255, 255, 255)
    step = max(1, n_px // 512) * 3
    counts = {}
    sums = {}
    for idx in range(0, len(raw) - 2, step):
        r, g, b = raw[idx], raw[idx + 1], raw[idx + 2]
        key = (r >> 3, g >> 3, b >> 3)
        counts[key] = counts.get(key, 0) + 1
        acc = sums.get(key)
        if acc is None:
            sums[key] = [r, g, b]
        else:
            acc[0] += r
            acc[1] += g
            acc[2] += b
    best_key = max(counts, key=counts.__getitem__)
    cnt = counts[best_key]
    sr, sg, sb = sums[best_key]
    return (int(round(sr / cnt)), int(round(sg / cnt)), int(round(sb / cnt)))


def _pad_pdf_page(crop, pad_top, pad_bot):
    """Add generous top/bottom margin matching the page background so breaks breathe."""
    if pad_top <= 0 and pad_bot <= 0:
        return crop
    width, height = crop.size
    top_rgb = _edge_fill_rgb(crop, 0, band_px=min(4, height))
    bot_rgb = _edge_fill_rgb(crop, max(0, height - 4), band_px=min(4, height))
    padded = Image.new("RGB", (width, height + pad_top + pad_bot), top_rgb)
    if pad_bot > 0 and bot_rgb != top_rgb:
        padded.paste(
            Image.new("RGB", (width, pad_bot), bot_rgb),
            (0, pad_top + height),
        )
    padded.paste(crop, (0, pad_top))
    return padded


def find_quiet_split(img, target, window=180):
    """Pick the center of a contiguous blank/low-content band near target for PDF page breaks."""
    height = img.size[1]
    low = max(1, target - window)
    high = min(height - 1, target + window)
    return find_quiet_band_center(
        img,
        low,
        high,
        prefer_y=target,
        band_rows=STITCH_SEAM_BAND,
    )


def _slice_pdf_pages(rgb, max_page):
    """Slice the continuous PNG into PDF pages without cutting through illustrations,
    and add clean head/foot breathing room around every page break."""
    width, height = rgb.size
    pages = []
    tail_pad = min(420, max_page // 5)
    split_win = min(320, max_page // 5)
    # Safe ceiling still comfortably below Pillow/PDF stream limits (~4200 px).
    hard_cap = min(max(max_page + 1200, int(max_page * 1.45)), 4050)
    min_early = max(620, int(max_page * 0.30))
    margin_px = min(max(int(round(width * 0.032)), 44), 92)
    y = 0
    prev_was_clean_break = False
    while y < height:
        remaining = height - y
        if remaining <= max_page + tail_pad:
            crop = rgb.crop((0, y, width, height))
            pad_top = margin_px if prev_was_clean_break else 0
            pages.append(_pad_pdf_page(crop, pad_top, margin_px // 2))
            break

        target = y + max_page
        low = max(y + 240, target - split_win)
        high = min(height - 1, target + split_win)
        split, raw_cost = find_quiet_band_center(
            rgb,
            low,
            high,
            prefer_y=target,
            band_rows=STITCH_SEAM_BAND,
            return_score=True,
        )

        # If the normal window lands inside an illustration / photo (high raw_cost),
        # look ahead up to `hard_cap` (after the image) or back up to `min_early`
        # (before the image) so illustrations stay whole on one PDF page!
        if raw_cost > 35.0:
            best_alt_y = None
            best_alt_cost = raw_cost
            best_alt_dist = 1e9

            early_lo = y + min_early
            early_hi = low
            if early_hi - early_lo >= STITCH_SEAM_BAND + 4:
                ey, ec = find_quiet_band_center(
                    rgb,
                    early_lo,
                    early_hi,
                    prefer_y=early_hi,
                    band_rows=STITCH_SEAM_BAND,
                    return_score=True,
                )
                if ec <= 25.0:
                    best_alt_y = ey
                    best_alt_cost = ec
                    best_alt_dist = abs(ey - target)

            late_lo = high
            late_hi = min(height - 1, y + hard_cap)
            if late_hi - late_lo >= STITCH_SEAM_BAND + 4:
                ly, lc = find_quiet_band_center(
                    rgb,
                    late_lo,
                    late_hi,
                    prefer_y=late_lo,
                    band_rows=STITCH_SEAM_BAND,
                    return_score=True,
                )
                if lc <= 25.0 and abs(ly - target) < best_alt_dist:
                    best_alt_y = ly
                    best_alt_cost = lc

            if best_alt_y is not None:
                split = best_alt_y
                raw_cost = best_alt_cost

        if split <= y + 240:
            split = min(y + max_page, height)
            raw_cost = 999.0

        crop = rgb.crop((0, y, width, split))
        clean_break = raw_cost <= 35.0
        pad_top = margin_px if prev_was_clean_break else 0
        pad_bot = margin_px if clean_break else 0
        pages.append(_pad_pdf_page(crop, pad_top, pad_bot))
        prev_was_clean_break = clean_break
        y = split
    return pages


def _pdf_footer_font(size_px):
    for name in ("segoeui.ttf", "arial.ttf", "calibri.ttf"):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            pass
    win_fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in ("segoeui.ttf", "arial.ttf", "calibri.ttf"):
        candidate = os.path.join(win_fonts, name)
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size_px)
            except OSError:
                pass
    return ImageFont.load_default()


def _stamp_pdf_page_numbers(pages):
    """Stamp subtle 'Page X of N' in the bottom-right footer margin on multi-page PDFs."""
    total = len(pages)
    if total < 2:
        return pages
    stamped = []
    for idx, page in enumerate(pages, 1):
        width, height = page.size
        bot_rgb = _edge_fill_rgb(page, max(0, height - 6), band_px=min(6, height))
        # Confirm the bottom strip is clean margin before drawing so artwork is never defaced.
        if _row_var(page, max(0, height - 14), int(width * 0.70), int(width * 0.96)) > 35.0:
            stamped.append(page)
            continue
        out = page.copy()
        draw = ImageDraw.Draw(out)
        size_px = min(max(int(round(width * 0.011)), 15), 26)
        font = _pdf_footer_font(size_px)
        lum = 0.299 * bot_rgb[0] + 0.587 * bot_rgb[1] + 0.114 * bot_rgb[2]
        # Subtle muted ink blended toward the background so it never shouts.
        target = 45 if lum > 140 else 210
        ink = tuple(int(round(c * 0.52 + target * 0.48)) for c in bot_rgb)
        label = f"Page {idx} of {total}"
        try:
            bbox = draw.textbbox((0, 0), label, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception:
            tw = len(label) * (size_px // 2 + 1)
            th = size_px
        x = max(16, int(width * 0.96) - tw)
        y = max(4, height - th - max(12, size_px // 2 + 4))
        draw.text((x, y), label, fill=ink, font=font)
        stamped.append(out)
    return stamped


def _pdf_escape_string(text):
    cleaned = "".join(ch for ch in (text or "") if 32 <= ord(ch) <= 126)
    return cleaned.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def export_pdf(image, pdf_path, title=None):
    """Write a paged PDF from the long PNG without shrinking or JPEG-blurring text."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    max_page = min(max(int(width * 11 / 8.5), 1400), 2800)
    pages = _slice_pdf_pages(rgb, max_page)
    if not pages:
        raise ValueError("Nothing to export.")
    pages = _stamp_pdf_page_numbers(pages)
    doc_title = title or os.path.splitext(os.path.basename(pdf_path))[0].replace("_", " ")
    dpi = max(96.0, min(220.0, width / 8.5))
    try:
        _write_flate_pdf(pages, pdf_path, dpi, title=doc_title)
        return len(pages)
    except Exception:
        return _write_jpeg_pdf(pages, pdf_path, dpi, title=doc_title)


def _write_flate_pdf(pages, path, dpi, title=None):
    """Embed RGB pages with lossless Flate so text stays sharp."""
    objects = [None, None, None]
    page_ids = []
    for image in pages:
        page = image.convert("RGB")
        width, height = page.size
        raw = zlib.compress(page.tobytes(), 6)
        img_id = len(objects)
        objects.append(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Interpolate true "
                f"/Filter /FlateDecode /Length {len(raw)} >>\nstream\n"
            ).encode("ascii")
            + raw
            + b"\nendstream"
        )
        page_w = width * 72.0 / dpi
        page_h = height * 72.0 / dpi
        content = f"q {page_w:.4f} 0 0 {page_h:.4f} 0 0 cm /Im0 Do Q\n".encode("ascii")
        content_id = len(objects)
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream"
        )
        page_id = len(objects)
        page_ids.append(page_id)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.4f} {page_h:.4f}] "
                f"/Resources << /XObject << /Im0 {img_id} 0 R >> /ProcSet [/PDF /ImageC] >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[2] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode("ascii")
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    info_id = len(objects)
    safe_title = _pdf_escape_string(title or APP_NAME)
    objects.append(
        f"<< /Title ({safe_title}) /Creator ({APP_NAME} — Crafted by Brass Silver) >>".encode("ascii")
    )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index in range(1, len(objects)):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(objects[index])
        output.extend(b"\nendobj\n")
    xref_at = len(output)
    output.extend(f"xref\n0 {len(objects)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer << /Size {len(objects)} /Root 1 0 R /Info {info_id} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n"
        ).encode("ascii")
    )
    with open(path, "wb") as handle:
        handle.write(output)


def _write_jpeg_pdf(pages, path, dpi, title=None):
    save_kw = dict(
        format="PDF",
        resolution=dpi,
        save_all=True,
        append_images=pages[1:],
        quality=95,
        subsampling=0,
        title=title or APP_NAME,
    )
    try:
        pages[0].save(path, **save_kw)
        return len(pages)
    except OSError:
        shorter = []
        for page in pages:
            pw, ph = page.size
            if ph <= 1100:
                shorter.append(page)
                continue
            for top in range(0, ph, 1000):
                shorter.append(page.crop((0, top, pw, min(top + 1000, ph))))
        if not shorter:
            raise
        shorter[0].save(
            path,
            format="PDF",
            resolution=dpi,
            save_all=True,
            append_images=shorter[1:],
            quality=95,
            subsampling=0,
        )
        return len(shorter)


def existing_long_image(folder):
    stem = harvest_file_stem(folder) or "stitched"
    for name in (f"{stem}.png", "stitched.png"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path, stem
    return None, stem


def harvest_outputs(folder):
    """Return (png_path or None, pdf_path or None, stem)."""
    png_path, stem = existing_long_image(folder)
    pdf_path = None
    for name in (f"{stem}.pdf", "stitched.pdf"):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            pdf_path = candidate
            break
    return png_path, pdf_path, stem


def list_recent_harvests(root, limit=RECENT_HARVEST_LIMIT):
    """Dated harvest folders in the save directory, newest first."""
    if not root or not os.path.isdir(root):
        return []
    rows = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in names:
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        parts = name.rsplit("_", 2)
        if (
            len(parts) != 3
            or not parts[1].isdigit()
            or len(parts[1]) != 8
            or not parts[2].isdigit()
            or len(parts[2]) != 6
        ):
            continue
        png_path, pdf_path, _stem = harvest_outputs(folder)
        title = parts[0] if parts[0] != "Harvest" else "Untitled"
        stamp = parts[1] + parts[2]
        try:
            when = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%b %d, %Y  %I:%M %p")
        except ValueError:
            when = f"{parts[1]} {parts[2]}"
        rows.append({
            "folder": folder,
            "name": title,
            "when": when,
            "sort": stamp,
            "png": png_path,
            "pdf": pdf_path,
        })
    rows.sort(key=lambda row: row["sort"], reverse=True)
    return rows[:limit]


def export_pdf_for_folder(folder):
    png_path, stem = existing_long_image(folder)
    if not png_path:
        return None, "No finished image found in that folder yet."
    image = Image.open(png_path)
    try:
        pdf_path = os.path.join(folder, f"{stem}.pdf")
        pages = export_pdf(image, pdf_path)
    finally:
        image.close()
    return pdf_path, (
        f"Saved {stem}.pdf ({pages} page{'s' if pages != 1 else ''}) from the existing image."
        f"{cleanup_note(cleanup_capture_frames(folder))}"
    )


def harvest_file_stem(folder):
    """'Neverbound_ch3_20260831_120000' -> 'Neverbound_ch3'. Timestamp-only folders return None."""
    base = os.path.basename(os.path.normpath(folder))
    parts = base.rsplit("_", 2)
    if (
        len(parts) == 3
        and parts[1].isdigit()
        and len(parts[1]) == 8
        and parts[2].isdigit()
        and len(parts[2]) == 6
        and parts[0]
        and parts[0] != "Harvest"
    ):
        return parts[0]
    return None


def sanitize_harvest_name(name):
    text = (name or "").strip()
    for character in '<>:"/\\|?*':
        text = text.replace(character, "")
    text = "".join(ch for ch in text if ord(ch) >= 32)
    text = "_".join(text.split())
    return text[:60].strip(" ._")


def app_base_dir():
    """Folder of the .exe when frozen, otherwise the script folder."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """Assets next to the script, or in the PyInstaller extract folder."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, *parts)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Desktop", APP_NAME)


def settings_file():
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(root, APP_NAME)
    else:
        folder = os.path.join(os.path.expanduser("~"), ".scrollharvest")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.json")


def load_settings():
    try:
        with open(settings_file(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _clamp_int(value, low, high, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _esc_pressed_since_last_check():
    """Detect a fresh Escape press globally on Windows without stealing focus."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8001)
    except Exception:
        return False


def _clamp_float(value, low, high, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


class ScrollHarvestApp:
    def __init__(self, root):
        self.root = root
        self.root.configure(bg=THEME["bg"])
        self.root.title(APP_NAME)
        self.root.resizable(True, True)
        self.set_app_icon()

        self.region = None
        self.running = False
        self.worker = None
        saved = load_settings()
        default_dir = default_output_dir()
        chosen_dir = saved.get("output_dir")
        self.output_dir = chosen_dir if chosen_dir and os.path.isdir(chosen_dir) else default_dir
        self.saved_scroll_steps = _clamp_int(saved.get("scroll_steps", DEFAULT_SCROLL_STEPS), 1, 200, DEFAULT_SCROLL_STEPS)
        self.saved_max_shots = _clamp_int(saved.get("max_shots", DEFAULT_MAX_SHOTS), 20, 2000, DEFAULT_MAX_SHOTS)
        self.pending_region = saved.get("region")

        self.status_var = tk.StringVar(value="Choose an area to harvest.")
        self.region_var = tk.StringVar(value="No capture area selected")
        self.delay_var = tk.DoubleVar(value=_clamp_float(saved.get("delay", 0.8), 0.2, 5.0, 0.8))
        self.scroll_steps_var = tk.IntVar(value=self.saved_scroll_steps)
        self.max_shots_var = tk.IntVar(value=self.saved_max_shots)
        self.pause_title_var = tk.StringVar(value="Paused because you moved the mouse.")
        self.pause_detail_var = tk.StringVar(
            value="Continue harvesting, or stop and keep the captures so far?"
        )
        self.complete_title_var = tk.StringVar(value="")
        self.complete_detail_var = tk.StringVar(value="")
        self.last_png = None
        self.last_pdf = None
        self.last_folder = None
        direction = saved.get("direction", "down")
        self.direction_var = tk.StringVar(value=direction if direction in ("down", "up") else "down")
        self.similarity_var = tk.DoubleVar(value=_clamp_float(saved.get("similarity", 1.2), 0.2, 20.0, 1.2))
        self.name_var = tk.StringVar(value=saved.get("harvest_name", ""))

        self.build_ui()
        self.apply_scroll_steps(self.saved_scroll_steps)
        self.apply_max_shots(self.saved_max_shots)
        self.root.after_idle(lambda: self.apply_scroll_steps(self.saved_scroll_steps))
        self.root.after_idle(lambda: self.apply_max_shots(self.saved_max_shots))
        self.root.after_idle(self.apply_saved_region)
        self.fit_window()
        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<FocusIn>", self._on_focus_in, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self.exclude_window_from_capture)

    def set_app_icon(self):
        ico_path = resource_path("scrollharvest_v2.ico")
        if not os.path.isfile(ico_path):
            ico_path = resource_path("scrollharvest.ico")
        png_path = resource_path("scrollharvest.png")
        if os.path.isfile(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except tk.TclError:
                pass
        if os.path.isfile(png_path):
            try:
                from PIL import ImageTk
                img = Image.open(png_path).convert("RGBA").resize((64, 64), RESAMPLE)
                photo = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, photo)
                self.root._icon_photo = photo
            except Exception:
                pass

    def paint_button(self, button, kind="secondary"):
        if kind == "start":
            button.configure(
                bg=THEME["start_bg"],
                fg=THEME["start_fg"],
                activebackground=THEME["gold"],
                activeforeground=THEME["start_fg"],
                disabledforeground=THEME["gold_dim"],
                font=("Segoe UI", 12, "bold"),
                relief="flat",
                bd=0,
                padx=22,
                pady=9,
                cursor="hand2",
                highlightthickness=0,
            )
        elif kind == "stop":
            button.configure(
                bg=THEME["stop_bg"],
                fg=THEME["fg"],
                activebackground="#8a4f4f",
                activeforeground=THEME["fg"],
                disabledforeground=THEME["muted"],
                font=("Segoe UI", 9),
                relief="flat",
                bd=0,
                padx=10,
                pady=6,
                cursor="hand2",
                highlightthickness=0,
            )
        elif kind == "finish":
            button.configure(
                bg="#2f543b",
                fg=THEME["fg"],
                activebackground="#3d6b4c",
                activeforeground=THEME["fg"],
                disabledforeground=THEME["muted"],
                font=("Segoe UI", 9, "bold"),
                relief="flat",
                bd=0,
                padx=12,
                pady=6,
                cursor="hand2",
                highlightthickness=1,
                highlightbackground=THEME["gold_dim"],
            )
        elif kind == "trash":
            button.configure(
                bg=THEME["panel"],
                fg=THEME["bad"],
                activebackground=THEME["stop_bg"],
                activeforeground=THEME["fg"],
                disabledforeground=THEME["muted"],
                font=("Segoe UI", 8),
                relief="flat",
                bd=0,
                padx=6,
                pady=2,
                cursor="hand2",
                highlightthickness=1,
                highlightbackground=THEME["stop_bg"],
            )
        elif kind == "chip":
            button.configure(
                bg=THEME["raised"],
                fg=THEME["fg"],
                activebackground=THEME["gold_dim"],
                activeforeground=THEME["fg"],
                disabledforeground=THEME["muted"],
                font=("Segoe UI", 8),
                relief="flat",
                bd=0,
                padx=6,
                pady=2,
                cursor="hand2",
                highlightthickness=0,
            )
        else:
            button.configure(
                bg=THEME["raised"],
                fg=THEME["fg"],
                activebackground=THEME["gold_dim"],
                activeforeground=THEME["fg"],
                disabledforeground=THEME["muted"],
                font=("Segoe UI", 9),
                relief="flat",
                bd=0,
                padx=8,
                pady=5,
                cursor="hand2",
                highlightthickness=0,
            )
        return button

    def paint_entry(self, widget):
        widget.configure(
            bg=THEME["entry_bg"],
            fg=THEME["fg"],
            insertbackground=THEME["gold"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["gold"],
            bd=0,
        )
        return widget

    def apply_scroll_steps(self, steps):
        steps = _clamp_int(steps, 1, 200, DEFAULT_SCROLL_STEPS)
        self.scroll_steps_box.delete(0, "end")
        self.scroll_steps_box.insert(0, str(steps))
        self.scroll_steps_var.set(steps)

    def apply_max_shots(self, shots):
        shots = _clamp_int(shots, 20, 2000, DEFAULT_MAX_SHOTS)
        self.max_shots_box.delete(0, "end")
        self.max_shots_box.insert(0, str(shots))
        self.max_shots_var.set(shots)

    def collect_settings(self):
        return {
            "output_dir": self.output_dir,
            "delay": float(self.delay_var.get()),
            "scroll_steps": _clamp_int(self.scroll_steps_var.get(), 1, 200, DEFAULT_SCROLL_STEPS),
            "max_shots": _clamp_int(self.max_shots_var.get(), 20, 2000, DEFAULT_MAX_SHOTS),
            "direction": self.direction_var.get(),
            "similarity": float(self.similarity_var.get()),
            "harvest_name": self.name_var.get().strip(),
            "region": list(self.region) if self.region else None,
        }

    def save_settings(self):
        try:
            with open(settings_file(), "w", encoding="utf-8") as handle:
                json.dump(self.collect_settings(), handle, indent=2)
        except OSError:
            pass

    def on_close(self):
        self.save_settings()
        self.root.destroy()

    def build_ui(self):
        t = THEME
        self.frame = tk.Frame(self.root, padx=22, pady=16, bg=t["bg"])
        self.frame.pack(fill="both", expand=True)

        tk.Label(
            self.frame, text="🌾  ScrollHarvest", font=("Segoe UI", 18, "bold"),
            bg=t["bg"], fg=t["gold"]
        ).pack(anchor="w")
        tk.Label(
            self.frame,
            text="If you can scroll it, you can save it.",
            font=("Segoe UI", 10),
            bg=t["bg"],
            fg=t["muted"],
        ).pack(anchor="w", pady=(0, 12))

        buttons = tk.Frame(self.frame, bg=t["bg"])
        buttons.pack(fill="x", pady=(0, 8))
        self.button_row = buttons

        self.start_btn = tk.Button(buttons, text="Start Harvest", command=self.start_capture)
        self.paint_button(self.start_btn, "start").pack(side="left", padx=(0, 4))
        select_btn = tk.Button(buttons, text="Select Capture Area", command=self.select_region)
        self.paint_button(select_btn).pack(side="left", padx=(10, 0))
        self.stop_btn = tk.Button(buttons, text="Stop", command=self.stop_capture, state="disabled")
        self.paint_button(self.stop_btn, "stop").pack(side="left", padx=(8, 0))

        self.pause_bar = tk.Frame(self.frame, padx=10, pady=8, bg=t["panel"], highlightthickness=1, highlightbackground=t["gold"])
        tk.Label(
            self.pause_bar, textvariable=self.pause_title_var,
            font=("Segoe UI", 10, "bold"), bg=t["panel"], fg=t["gold"]
        ).pack(anchor="w")
        tk.Label(
            self.pause_bar, textvariable=self.pause_detail_var,
            bg=t["panel"], fg=t["fg"]
        ).pack(anchor="w", pady=(2, 8))
        pause_buttons = tk.Frame(self.pause_bar, bg=t["panel"])
        pause_buttons.pack(anchor="w")
        cont_btn = tk.Button(pause_buttons, text="Continue", width=13, command=self.continue_harvest)
        self.paint_button(cont_btn, "start").pack(side="left")
        pause_finish = tk.Button(pause_buttons, text="Finish & Save", width=14, command=self.stop_capture)
        self.paint_button(pause_finish, "finish").pack(side="left", padx=(8, 0))
        pause_discard = tk.Button(pause_buttons, text="🗑  Discard", command=self.discard_capture)
        self.paint_button(pause_discard, "stop").pack(side="left", padx=(16, 0))
        self.pause_event = None
        self.pause_choice = False
        self.discard_requested = False

        self.complete_bar = tk.Frame(self.frame, bg=t["panel"], highlightthickness=1, highlightbackground=t["gold"])
        self.complete_accent = tk.Frame(self.complete_bar, width=5, bg=t["gold"])
        self.complete_accent.pack(side="left", fill="y")
        self.complete_accent.pack_propagate(False)
        complete_inner = tk.Frame(self.complete_bar, padx=12, pady=10, bg=t["panel"])
        complete_inner.pack(side="left", fill="both", expand=True)
        self.complete_title_label = tk.Label(
            complete_inner, textvariable=self.complete_title_var,
            font=("Segoe UI", 12, "bold"), bg=t["panel"], fg=t["gold"]
        )
        self.complete_title_label.pack(anchor="w")
        self.complete_detail_label = tk.Label(
            complete_inner, textvariable=self.complete_detail_var,
            bg=t["panel"], fg=t["fg"], justify="left"
        )
        self.complete_detail_label.pack(anchor="w", pady=(4, 8))
        complete_buttons = tk.Frame(complete_inner, bg=t["panel"])
        complete_buttons.pack(anchor="w")
        self.complete_pdf_btn = tk.Button(complete_buttons, text="Open PDF", command=self.open_last_pdf)
        self.paint_button(self.complete_pdf_btn).pack(side="left")
        self.complete_png_btn = tk.Button(complete_buttons, text="Open PNG", command=self.open_last_png)
        self.paint_button(self.complete_png_btn).pack(side="left", padx=(8, 0))
        self.complete_folder_btn = tk.Button(complete_buttons, text="Open Folder", command=self.open_last_folder)
        self.paint_button(self.complete_folder_btn).pack(side="left", padx=(8, 0))

        self.region_label = tk.Label(
            self.frame, textvariable=self.region_var, bg=t["bg"], fg=t["muted"]
        )
        self.region_label.pack(anchor="w", pady=(0, 8))

        name_row = tk.Frame(self.frame, bg=t["bg"])
        name_row.pack(fill="x", pady=(0, 10))
        tk.Label(name_row, text="Harvest name:", bg=t["bg"], fg=t["fg"]).pack(side="left")
        self.name_entry = tk.Entry(name_row, textvariable=self.name_var)
        self.paint_entry(self.name_entry).pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)

        tk.Label(
            self.frame, text="Settings", font=("Segoe UI", 10, "bold"),
            bg=t["bg"], fg=t["gold"]
        ).pack(anchor="w", pady=(4, 4))
        settings = tk.Frame(self.frame, bg=t["bg"])
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        def add_setting(row, label, widget):
            tk.Label(settings, text=label, bg=t["bg"], fg=t["fg"]).grid(
                row=row, column=0, sticky="w", pady=6
            )
            widget.grid(row=row, column=1, sticky="w", padx=(14, 0), pady=6)

        delay_box = tk.Spinbox(
            settings, from_=0.2, to=5.0, increment=0.1, textvariable=self.delay_var, width=8
        )
        self.paint_entry(delay_box)
        add_setting(0, "Pause after scroll (sec):", delay_box)
        self.scroll_steps_box = tk.Spinbox(
            settings, from_=1, to=200, increment=1,
            textvariable=self.scroll_steps_var, width=8
        )
        self.paint_entry(self.scroll_steps_box)
        add_setting(1, "Scroll steps:", self.scroll_steps_box)
        direction_menu = tk.OptionMenu(settings, self.direction_var, "down", "up")
        direction_menu.configure(
            bg=t["raised"], fg=t["fg"], activebackground=t["gold_dim"],
            activeforeground=t["fg"], highlightthickness=0, bd=0,
            relief="flat", font=("Segoe UI", 9)
        )
        try:
            direction_menu["menu"].configure(bg=t["panel"], fg=t["fg"], activebackground=t["gold_dim"])
        except tk.TclError:
            pass
        add_setting(2, "Scroll direction:", direction_menu)
        sim_box = tk.Spinbox(
            settings, from_=0.2, to=20.0, increment=0.2, textvariable=self.similarity_var, width=8
        )
        self.paint_entry(sim_box)
        add_setting(3, "Stop when change ≤:", sim_box)
        self.max_shots_box = tk.Spinbox(
            settings, from_=20, to=2000, increment=50,
            textvariable=self.max_shots_var, width=8
        )
        self.paint_entry(self.max_shots_box)
        add_setting(4, "Max shots:", self.max_shots_box)

        self.hint_label = tk.Label(
            self.frame,
            text="Lower scroll steps if stitch tears. Max shots pauses a runaway harvest.",
            bg=t["bg"],
            fg=t["muted"],
            justify="left"
        )
        self.hint_label.pack(anchor="w", pady=(6, 8))

        folder_row = tk.Frame(self.frame, bg=t["bg"])
        folder_row.pack(fill="x", pady=(0, 8))
        choose_btn = tk.Button(folder_row, text="Choose Save Folder", command=self.choose_folder)
        self.paint_button(choose_btn).pack(side="left")
        open_btn = tk.Button(folder_row, text="Open Folder", command=self.open_save_folder)
        self.paint_button(open_btn).pack(side="left", padx=(8, 0))
        stitch_btn = tk.Button(folder_row, text="Stitch Folder", command=self.stitch_chosen_folder)
        self.paint_button(stitch_btn).pack(side="left", padx=(8, 0))
        self.folder_label = tk.Label(
            self.frame, text=self.output_dir, bg=t["bg"], fg=t["muted"],
            justify="left", anchor="w"
        )
        self.folder_label.pack(fill="x", pady=(4, 12))

        self.status_label = tk.Label(
            self.frame,
            textvariable=self.status_var,
            font=("Segoe UI", 10, "bold"),
            bg=t["bg"],
            fg=t["gold"],
            justify="left"
        )
        self.status_label.pack(anchor="w", pady=(0, 10))

        self.recent_header = tk.Label(
            self.frame, text="Recent harvests", font=("Segoe UI", 10, "bold"),
            bg=t["bg"], fg=t["gold"]
        )
        self.recent_header.pack(anchor="w", pady=(2, 0))
        self.recent_box = tk.Frame(self.frame, bg=t["bg"])
        self.recent_box.pack(fill="x", pady=(6, 0))

        self.tip_label = tk.Label(
            self.frame,
            text="Move the mouse or press Esc during a harvest to pause. Finished files stay until you open them — nothing launches on its own.",
            bg=t["bg"],
            fg=t["muted"],
            justify="left"
        )
        self.tip_label.pack(anchor="w", pady=(10, 4))
        tk.Label(
            self.frame,
            text="Crafted by Brass Silver",
            font=("Segoe UI", 8, "italic"),
            bg=t["bg"],
            fg=t["gold_dim"],
        ).pack(anchor="e", pady=(6, 0))
        self.refresh_recent_harvests()

    def _wrap_labels(self):
        return (
            self.hint_label, self.folder_label, self.status_label, self.tip_label,
            self.complete_detail_label, self.region_label,
        )

    def _wrap_width(self):
        width = self.frame.winfo_width()
        if width < 80:
            return 620
        return max(width - 24, 400)

    def _on_resize(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        wrap = self._wrap_width()
        if getattr(self, "_last_wrap", None) == wrap:
            return
        self._last_wrap = wrap
        for label in self._wrap_labels():
            label.configure(wraplength=wrap)

    def fit_window(self):
        self._last_wrap = 620
        for label in self._wrap_labels():
            label.configure(wraplength=self._last_wrap)
        self.root.update_idletasks()
        width = max(self.frame.winfo_reqwidth() + 48, 760)
        height = self.frame.winfo_reqheight() + 52
        screen_h = self.root.winfo_screenheight()
        height = min(height, max(int(screen_h * 0.88), 560))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(720, 560)

    def park_window(self):
        """Keep controls visible without covering the middle of the capture area."""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        screen_w = self.root.winfo_screenwidth()
        x = max(screen_w - width - 24, 0)
        self.root.geometry(f"{width}x{height}+{x}+24")
        self.root.lift()
        self.root.attributes("-topmost", True)

    def restore_main_window(self):
        self.root.deiconify()
        self.park_window()
        self.root.after(400, lambda: self.root.attributes("-topmost", False) if not self.running else None)

    def select_region(self):
        self.close_extra_windows()
        self.root.withdraw()
        RegionSelector(self.root, self.region_selected, last_region=self.region)

    def close_extra_windows(self):
        for child in list(self.root.winfo_children()):
            if isinstance(child, tk.Toplevel):
                try:
                    child.destroy()
                except tk.TclError:
                    pass
        try:
            close_stray_scrollharvest_windows(keep_hwnd=tk_root_hwnd(self.root))
        except Exception:
            pass

    def apply_saved_region(self):
        region = normalize_region(self.pending_region, self.root)
        self.pending_region = None
        if not region:
            return
        self.set_region(region, saved=True)
        self.status_var.set("Last capture area restored. Start Harvest, or Select Capture Area to change it.")

    def set_region(self, region, saved=False):
        self.region = region
        self.region_var.set(describe_region(region, saved=saved))

    def region_selected(self, region):
        self.restore_main_window()
        if region is None:
            return
        self.set_region(region, saved=False)
        self.save_settings()

    def choose_folder(self):
        chosen = filedialog.askdirectory(parent=self.root, initialdir=self.output_dir)
        if chosen:
            self.output_dir = chosen
            self.folder_label.config(text=self.output_dir)
            self.save_settings()
            self.refresh_recent_harvests()

    def latest_save_path(self):
        if getattr(self, "session_dir", None) and os.path.isdir(self.session_dir):
            return self.session_dir
        return self.output_dir

    def stitch_chosen_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root,
            initialdir=self.output_dir,
            mustexist=True,
            title="Select a harvest folder (stay in ScrollHarvest — click the dated folder, don't open it)"
        )
        if not folder:
            return
        try:
            png_path, _stem = existing_long_image(folder)
            if png_path:
                self.status_var.set("Building PDF from the existing image…")
                self.root.update_idletasks()
                path, message = export_pdf_for_folder(folder)
            else:
                self.status_var.set("Stitching captures…")
                self.root.update_idletasks()
                path, message = stitch_captures(folder, self.direction_var.get())
        except Exception as exc:
            self.status_var.set(f"Stitch/PDF failed: {exc}")
            messagebox.showerror(APP_NAME, f"Could not stitch that folder:\n{exc}")
            return
        self.show_completion(
            "Stitch Folder",
            None,
            folder,
            path,
            message or "",
        )

    def _recent_signature(self, rows):
        return (
            self.running,
            tuple((row["folder"], bool(row["png"]), bool(row["pdf"])) for row in rows),
        )

    def _on_focus_in(self, _event=None):
        # Rebuild only when something changed on disk, so a click that also
        # focuses the window still lands on its button.
        rows = list_recent_harvests(self.output_dir)
        if self._recent_signature(rows) != getattr(self, "_recent_sig", None):
            self.refresh_recent_harvests()

    def refresh_recent_harvests(self):
        for child in self.recent_box.winfo_children():
            child.destroy()
        t = THEME
        rows = list_recent_harvests(self.output_dir)
        self._recent_sig = self._recent_signature(rows)
        if not rows:
            tk.Label(
                self.recent_box,
                text="No harvests in this save folder yet.",
                bg=t["bg"],
                fg=t["muted"],
            ).pack(anchor="w")
            return
        for index, row in enumerate(rows):
            shade = t["panel"] if index % 2 == 0 else t["panel_alt"]
            card = tk.Frame(self.recent_box, bg=shade, padx=10, pady=7)
            card.pack(fill="x", pady=(0, 3))
            card.columnconfigure(0, weight=1)
            flags = ("PNG" if row["png"] else "—") + "  " + ("PDF" if row["pdf"] else "—")
            info = tk.Frame(card, bg=shade)
            info.grid(row=0, column=0, sticky="ew")
            tk.Label(
                info,
                text=row["name"],
                font=("Segoe UI", 10, "bold"),
                bg=shade,
                fg=t["fg"],
                anchor="w",
            ).pack(side="left")
            tk.Label(
                info,
                text=f"   {row['when']}    {flags}",
                font=("Segoe UI", 9),
                bg=shade,
                fg=t["muted"],
                anchor="w",
            ).pack(side="left")
            actions = tk.Frame(card, bg=shade)
            actions.grid(row=0, column=1, sticky="e", padx=(10, 0))
            pdf_btn = tk.Button(
                actions, text="PDF",
                command=lambda p=row["pdf"]: self.open_named_file(p, "PDF"),
                state="normal" if row["pdf"] else "disabled",
            )
            self.paint_button(pdf_btn, "chip").pack(side="left")
            png_btn = tk.Button(
                actions, text="PNG",
                command=lambda p=row["png"]: self.open_named_file(p, "PNG"),
                state="normal" if row["png"] else "disabled",
            )
            self.paint_button(png_btn, "chip").pack(side="left", padx=(4, 0))
            rebuild_btn = tk.Button(
                actions, text="Rebuild PDF",
                command=lambda f=row["folder"]: self.rebuild_recent_pdf(f),
                state="normal" if row["png"] else "disabled",
            )
            self.paint_button(rebuild_btn, "chip").pack(side="left", padx=(4, 0))
            fold_btn = tk.Button(
                actions, text="Folder",
                command=lambda f=row["folder"]: self.open_named_folder(f),
            )
            self.paint_button(fold_btn, "chip").pack(side="left", padx=(4, 0))
            trash_btn = tk.Button(
                actions, text="🗑",
                command=lambda f=row["folder"], n=row["name"], w=row["when"]: self.trash_harvest(f, n, w),
                state="disabled" if self.running else "normal",
            )
            self.paint_button(trash_btn, "trash").pack(side="left", padx=(10, 0))

    def trash_harvest(self, folder, name, when):
        if self.running:
            return
        if not os.path.isdir(folder):
            if self.last_folder and os.path.normcase(os.path.abspath(self.last_folder)) == os.path.normcase(os.path.abspath(folder)):
                self.last_folder = self.last_png = self.last_pdf = None
                self.hide_completion()
            self.status_var.set(f"\"{name}\" was already deleted, so it was removed from the list.")
            self.refresh_recent_harvests()
            return
        if not is_harvest_folder(folder, self.output_dir):
            messagebox.showerror(APP_NAME, "That folder isn't a harvest in your save folder, so it was left alone.")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"Move \"{name}\" ({when.strip()}) to the Recycle Bin?\n\n"
            "This removes its PNG and PDF. You can restore it from the Recycle Bin.",
            icon="warning",
            default="no",
        ):
            return
        try:
            send_to_recycle_bin(folder)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not move it to the Recycle Bin:\n{exc}")
            return
        if self.last_folder and os.path.normcase(os.path.abspath(self.last_folder)) == os.path.normcase(os.path.abspath(folder)):
            self.last_folder = self.last_png = self.last_pdf = None
            self.hide_completion()
        self.status_var.set(f"Moved \"{name}\" to the Recycle Bin.")
        self.refresh_recent_harvests()

    def rebuild_recent_pdf(self, folder):
        if not folder or not os.path.isdir(folder):
            return
        try:
            self.status_var.set("Rebuilding PDF from the stitched image…")
            self.root.update_idletasks()
            path, message = export_pdf_for_folder(folder)
        except Exception as exc:
            self.status_var.set(f"PDF rebuild failed: {exc}")
            messagebox.showerror(APP_NAME, f"Could not rebuild PDF:\n{exc}")
            return
        self.show_completion(
            "Stitch Folder",
            None,
            folder,
            path,
            message or "",
        )

    def hide_live_status(self):
        try:
            self.status_label.pack_forget()
        except tk.TclError:
            pass

    def show_live_status(self):
        try:
            if not self.status_label.winfo_ismapped():
                self.status_label.pack(anchor="w", pady=(0, 10), before=self.recent_header)
        except tk.TclError:
            pass

    def hide_completion(self):
        try:
            self.complete_bar.pack_forget()
        except tk.TclError:
            pass
        self.show_live_status()

    def show_completion(self, summary, count, folder, stitch_path, stitch_msg, stitch_error=None):
        png_path, pdf_path, _stem = harvest_outputs(folder) if folder else (None, None, None)
        if stitch_path and os.path.isfile(stitch_path) and stitch_path.lower().endswith(".png"):
            png_path = stitch_path
        self.last_folder = folder
        self.last_png = png_path
        self.last_pdf = pdf_path

        png_ok = bool(png_path) and os.path.isfile(png_path)
        pdf_failed = bool(stitch_error) or (stitch_msg and "PDF failed" in stitch_msg)
        pdf_ok = bool(pdf_path) and os.path.isfile(pdf_path) and not pdf_failed
        stitch_folder = summary == "Stitch Folder"
        user_stopped = bool(summary) and str(summary).startswith("Stopped")

        if stitch_folder and png_ok and pdf_ok:
            title = "✓  Folder stitched"
            success = True
        elif stitch_folder and png_ok:
            title = "✓  Folder stitched"
            success = True
        elif stitch_folder:
            title = "Folder stitch incomplete"
            success = False
        elif png_ok and pdf_ok:
            title = "✓  Harvest complete"
            success = True
        elif png_ok:
            title = "✓  Harvest complete"
            success = True
        elif user_stopped:
            title = "Harvest stopped"
            success = False
        else:
            title = "Harvest stopped"
            success = False

        lines = []
        if count is not None:
            lines.append(f"{count} screen{'s' if count != 1 else ''} captured")
        if count is not None and count < 2:
            lines.append("Need at least two captures to stitch.")
        elif png_ok:
            lines.append("PNG stitched successfully")
        elif stitch_error:
            lines.append(f"PNG stitch failed: {stitch_error}")
        elif stitch_msg and "Need at least two" in stitch_msg:
            lines.append(stitch_msg)
        else:
            lines.append("PNG was not created")
        if pdf_ok:
            lines.append("PDF created successfully")
        elif png_ok and pdf_failed:
            extra = (
                stitch_msg.split("PDF failed:", 1)[-1].strip()
                if stitch_msg and "PDF failed" in stitch_msg
                else (stitch_error or "unknown error")
            )
            lines.append(f"PDF failed: {extra}")
        else:
            lines.append("PDF was not created")

        self.complete_title_var.set(title)
        self.complete_detail_var.set("\n".join(lines))
        title_color = THEME["gold"] if success else THEME["warn"]
        border = THEME["gold"] if success else THEME["gold_dim"]
        accent = THEME["gold"] if success else THEME["gold_dim"]
        self.complete_title_label.configure(fg=title_color)
        self.complete_bar.configure(highlightbackground=border)
        self.complete_accent.configure(bg=accent)
        self.complete_pdf_btn.config(state="normal" if pdf_path else "disabled")
        self.complete_png_btn.config(state="normal" if png_path else "disabled")
        self.complete_folder_btn.config(state="normal" if folder and os.path.isdir(folder) else "disabled")
        self.hide_live_status()
        if not self.complete_bar.winfo_ismapped():
            self.complete_bar.pack(fill="x", pady=(0, 10), after=self.button_row)
        self.refresh_recent_harvests()

    def open_named_file(self, path, kind):
        try:
            open_file(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open {kind}:\n{path}\n\n{exc}")

    def open_named_folder(self, folder):
        try:
            open_in_explorer(folder)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{folder}\n\n{exc}")

    def open_last_pdf(self):
        self.open_named_file(self.last_pdf, "PDF")

    def open_last_png(self):
        self.open_named_file(self.last_png, "PNG")

    def open_last_folder(self):
        self.open_named_folder(self.last_folder or self.latest_save_path())

    def open_save_folder(self, path=None):
        path = path or self.latest_save_path()
        try:
            open_in_explorer(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{path}\n\n{exc}")

    def scroll_clicks(self):
        steps = int(self.scroll_steps_var.get())
        if steps == 0:
            steps = 1
        if self.direction_var.get() == "up":
            return steps
        return -steps

    def start_capture(self):
        if not self.region:
            messagebox.showinfo(APP_NAME, "Select a capture area first.")
            return
        if self.running:
            return

        os.makedirs(self.output_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        clean_name = sanitize_harvest_name(self.name_var.get())
        folder_name = f"{clean_name}_{stamp}" if clean_name else f"Harvest_{stamp}"
        self.session_dir = os.path.join(self.output_dir, folder_name)
        os.makedirs(self.session_dir, exist_ok=True)

        self.running = True
        self.discard_requested = False
        self.refresh_recent_harvests()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.hide_completion()
        self.show_live_status()
        self.park_window()
        self.save_settings()
        self.status_var.set(
            f"Starting in 3 seconds. Click the page you want saved — "
            f"then move the mouse if you want to pause.\nSaving to: {self.session_dir}"
        )

        self.worker = threading.Thread(target=self.capture_loop, daemon=True)
        self.worker.start()

    def stop_capture(self):
        self.running = False
        self.status_var.set("Finishing and stitching captures…")
        self.finish_pause(continue_harvest=False)

    def discard_capture(self):
        if not messagebox.askyesno(
            APP_NAME,
            "Discard this harvest?\n\nThe screenshots taken so far will go to the Recycle Bin "
            "and no PNG or PDF will be made.",
            icon="warning",
            default="no",
        ):
            return
        self.discard_requested = True
        self.running = False
        self.status_var.set("Discarding this harvest…")
        self.finish_pause(continue_harvest=False)

    def continue_harvest(self):
        self.finish_pause(continue_harvest=True)

    def finish_pause(self, continue_harvest):
        self.pause_choice = continue_harvest
        try:
            self.pause_bar.pack_forget()
            self.stop_btn.config(text="Stop")
            self.paint_button(self.stop_btn, "stop")
        except tk.TclError:
            pass
        event = self.pause_event
        if event is not None:
            event.set()

    def exclude_window_from_capture(self):
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(
                toplevel_hwnd(self.root), WDA_EXCLUDEFROMCAPTURE
            )
        except Exception:
            pass

    def physical_region(self):
        return to_physical_bbox(self.region, self.root)

    def screenshot_region(self):
        """Move this app off-screen, freeze Tk so it cannot redraw, then grab."""
        bbox = self.physical_region()
        main_hwnd = toplevel_hwnd(self.root)
        gone = threading.Event()
        finished = threading.Event()
        positions = {}

        def park_offscreen():
            user32 = ctypes.windll.user32
            hwnds = list_scrollharvest_hwnds()
            if main_hwnd not in hwnds:
                hwnds.append(main_hwnd)
            flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSENDCHANGING
            for hwnd in hwnds:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    positions[hwnd] = (rect.left, rect.top)
                    user32.SetWindowPos(
                        hwnd, None, OFFSCREEN_POS, OFFSCREEN_POS, 0, 0, flags
                    )
            gone.set()
            # Block the UI thread until the grab is done so Tk cannot remap us.
            finished.wait(timeout=8)
            for hwnd, (x, y) in positions.items():
                user32.SetWindowPos(hwnd, None, x, y, 0, 0, flags)

        self.root.after(0, park_offscreen)
        if not gone.wait(timeout=3):
            raise RuntimeError("Could not move the ScrollHarvest window off the screen.")
        time.sleep(0.2)
        try:
            try:
                return ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                return ImageGrab.grab(bbox=bbox)
        finally:
            finished.set()

    def window_rect_physical(self):
        sx, sy = tk_to_physical_scale(self.root)
        left = int(round(self.root.winfo_rootx() * sx))
        top = int(round(self.root.winfo_rooty() * sy))
        right = left + int(round(self.root.winfo_width() * sx))
        bottom = top + int(round(self.root.winfo_height() * sy))
        return left, top, right, bottom

    def pointer_target(self):
        left, top, right, bottom = self.physical_region()
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        win = self.window_rect_physical()
        if win[0] <= cx <= win[2] and win[1] <= cy <= win[3]:
            cx = left + max((right - left) // 4, 40)
            cy = top + max((bottom - top) // 2, 40)
        return cx, cy

    def move_pointer_to_region(self):
        cx, cy = self.pointer_target()
        pyautogui.moveTo(cx, cy)
        return cx, cy

    def mouse_was_moved(self, placed_x, placed_y):
        pos = pyautogui.position()
        return (
            abs(pos.x - placed_x) > MOUSE_MOVE_PAUSE_PX
            or abs(pos.y - placed_y) > MOUSE_MOVE_PAUSE_PX
        )

    def ask_stop_or_continue(self, reason="mouse"):
        """Show Continue / Finish & Save on the main window until the user chooses."""
        self.pause_event = threading.Event()
        self.pause_choice = False

        def show_pause():
            if reason == "limit":
                self.pause_title_var.set(f"Paused at {int(self.max_shots_var.get())} shots.")
                self.pause_detail_var.set(
                    "Click Continue for another batch, or Finish & Save to stitch what you have."
                )
            elif reason == "esc":
                self.pause_title_var.set("Paused (Esc pressed).")
                self.pause_detail_var.set(
                    "Click Continue to keep harvesting, or Finish & Save to stitch what you have."
                )
            else:
                self.pause_title_var.set("Paused because you moved the mouse.")
                self.pause_detail_var.set(
                    "Click Continue to keep harvesting, or Finish & Save to stitch what you have."
                )
            self.stop_btn.config(text="Finish & Save")
            self.paint_button(self.stop_btn, "finish")
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            if not self.pause_bar.winfo_ismapped():
                self.pause_bar.pack(fill="x", pady=(0, 8), after=self.button_row)

        self.root.after(0, show_pause)
        self.pause_event.wait()
        # Drain any Esc press made while paused so resuming doesn't re-pause immediately.
        _esc_pressed_since_last_check()
        return self.pause_choice and self.running

    def notify_saved(self, summary, count):
        stitch_path = None
        stitch_msg = ""
        stitch_error = None
        folder = self.session_dir
        if count >= 2:
            self.set_status("Stitching captures into one image…")
            try:
                stitch_path, stitch_msg = stitch_captures(folder, self.direction_var.get())
            except Exception as exc:
                stitch_error = str(exc)
                stitch_msg = f"Stitch failed: {exc}"
        self.root.after(
            0,
            lambda: self.show_completion(summary, count, folder, stitch_path, stitch_msg, stitch_error),
        )

    @staticmethod
    def difference_score(img1, img2):
        diff = ImageChops.difference(img1.convert("RGB"), img2.convert("RGB"))
        stat = ImageStat.Stat(diff)
        return sum(stat.mean) / len(stat.mean)

    def capture_loop(self):
        unchanged_count = 0
        index = 1
        saved = 0
        finished_naturally = False
        try:
            _esc_pressed_since_last_check()
            for _ in range(30):
                if not self.running:
                    return
                if _esc_pressed_since_last_check():
                    self.running = False
                    self.set_status("Harvest cancelled before capture started.")
                    return
                time.sleep(0.1)
            previous = None
            placed = self.move_pointer_to_region()
            batch = _clamp_int(self.max_shots_var.get(), 20, 2000, DEFAULT_MAX_SHOTS)
            next_pause_at = batch

            while self.running:
                image = self.screenshot_region()
                path = os.path.join(self.session_dir, f"capture_{index:04d}.png")
                image.save(path)
                saved = index

                if previous is not None:
                    score = self.difference_score(previous, image)
                    threshold = float(self.similarity_var.get())

                    if score <= threshold:
                        unchanged_count += 1
                    else:
                        unchanged_count = 0

                    self.set_status(
                        f"Capturing {index}…  change score {score:.2f}  (stop at ≤ {threshold:.1f})\n"
                        f"Move the mouse or press Esc to pause. Saving to: {self.session_dir}"
                    )

                    if unchanged_count >= 2:
                        self.running = False
                        finished_naturally = True
                        break
                else:
                    self.set_status(
                        f"Capturing {index}…\nMove the mouse or press Esc to pause. Saving to: {self.session_dir}"
                    )

                previous = image.copy()
                index += 1

                if saved >= next_pause_at:
                    self.set_status(
                        f"Paused at {saved} shots. Continue or Finish & Save.\nSaving to: {self.session_dir}"
                    )
                    if not self.ask_stop_or_continue("limit"):
                        self.running = False
                        break
                    next_pause_at = saved + batch
                    placed = self.move_pointer_to_region()

                pyautogui.scroll(self.scroll_clicks())
                delay_total = max(0.2, float(self.delay_var.get()))
                slept = 0.0
                esc_hit = False
                while slept < delay_total and self.running:
                    if _esc_pressed_since_last_check():
                        esc_hit = True
                        break
                    chunk = min(0.08, delay_total - slept)
                    time.sleep(chunk)
                    slept += chunk

                if not self.running:
                    break

                if esc_hit:
                    self.set_status("Paused (Esc pressed).")
                    if not self.ask_stop_or_continue("esc"):
                        self.running = False
                        break
                    placed = self.move_pointer_to_region()
                elif self.mouse_was_moved(*placed):
                    self.set_status("Paused because you moved the mouse.")
                    if not self.ask_stop_or_continue("mouse"):
                        self.running = False
                        break
                    placed = self.move_pointer_to_region()

            if self.discard_requested:
                self.discard_session()
            elif finished_naturally:
                self.notify_saved(f"Finished. Content stopped changing after {saved} captures.", saved)
            elif saved:
                self.notify_saved("Stopped.", saved)

        except Exception as exc:
            self.running = False
            self.set_status(f"Capture stopped because of an error: {exc}")
        finally:
            self.root.after(0, self.reset_buttons)

    def discard_session(self):
        folder = self.session_dir
        message = "Harvest discarded."
        try:
            if folder and os.path.isdir(folder):
                send_to_recycle_bin(folder)
                message = "Harvest discarded — screenshots moved to the Recycle Bin."
        except Exception as exc:
            message = f"Harvest discarded, but its folder could not be recycled: {exc}"
        self.set_status(message)

    def set_status(self, text):
        self.root.after(0, lambda value=text: self.status_var.set(value))

    def reset_buttons(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled", text="Stop")
        self.paint_button(self.stop_btn, "stop")
        self.root.attributes("-topmost", False)
        self.finish_pause(continue_harvest=False)
        self.refresh_recent_harvests()


if __name__ == "__main__":
    root = tk.Tk()
    app = ScrollHarvestApp(root)
    root.mainloop()
