import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_SCRIPT = os.path.join(HERE, "denoise_video.py")
APP_TITLE = "โปรแกรมตัดเสียงรบกวน & บันทึกเสียง (Denoise & Audio Recorder)"


def _fatal(msg):
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_TITLE, msg)
        r.destroy()
    except Exception:  # noqa: BLE001
        print(msg, file=sys.stderr)
    sys.exit(1)


try:
    import denoise_common as C
except ImportError:
    _fatal("ไม่พบไฟล์ denoise_common.py ในโฟลเดอร์เดียวกับโปรแกรมนี้\n"
           "กรุณาคัดลอกไฟล์ทั้งหมดของโปรแกรมไปไว้ด้วยกัน")

try:
    import audio_recorder
except ImportError:
    audio_recorder = None

PREVIEW_SECONDS = 300


# ============================================================ logic (ไม่ใช้ tkinter)
def default_output(src, preview=False, target_dir=None):
    if not src:
        return ""
    base, ext = os.path.splitext(src)
    filename = os.path.basename(base)
    ext_lower = ext.lower()
    if ext_lower in C.AUDIO_ONLY_EXT:
        out_ext = ext_lower if ext_lower in (".wav", ".mp3", ".flac", ".m4a") else ".m4a"
    else:
        out_ext = ext or ".mp4"
    out_name = f"{filename}_{'preview' if preview else 'denoised'}{out_ext}"
    if target_dir and str(target_dir).strip():
        return os.path.join(str(target_dir).strip(), out_name)
    return os.path.join(os.path.dirname(src), out_name)


def build_command(o, src, dst):
    """o: dict ของตัวเลือก -> รายการอาร์กิวเมนต์ของ denoise_video.py
    ส่งเฉพาะตัวเลือกที่เอนจินนั้นรองรับ (อ่านจาก C.ENGINES)"""
    spec = C.ENGINES[o["engine"]]
    cmd = [sys.executable, ENGINE_SCRIPT, src, dst,
           "--engine", o["engine"], "--progress-json",
           "--audio-bitrate", o["bitrate"]]
    for key in spec["options"]:
        cmd += [C.OPTIONS[key]["flag"], str(o["values"][key])]
    if o.get("noise_manual") and spec.get("noise_profile"):
        cmd += ["--noise-start", str(o["noise_start"]),
                "--noise-dur", str(o["noise_dur"])]
    if o.get("video_denoise"):
        cmd.append("--video-denoise")
    if o.get("auto_enhance", True):
        cmd.append("--auto-enhance")
    if o.get("wind_reduction", True):
        cmd.append("--wind-reduction")
    if o.get("auto_tune", True) and o["engine"] == "spectral":
        cmd.append("--auto-tune")
    return cmd


_RE_STAGE = re.compile(r"\[(\d)/2\].*?(\d+(?:\.\d+)?)%")


def parse_progress(line):
    """คืน (เปอร์เซ็นต์รวม 0-100, ข้อความสถานะ) หรือ None ถ้าบรรทัดนั้นไม่ใช่ความคืบหน้า
    อ่านแบบ JSON ก่อน แล้วค่อยลองรูปแบบข้อความเดิมเป็นตัวสำรอง"""
    d = C.parse_progress_line(line)
    if d:
        return C.overall_percent(d), C.progress_text(d)
    m = _RE_STAGE.search(line)
    if m:
        stage, pct = int(m.group(1)), float(m.group(2))
        if stage == 1:
            return pct * 0.15, f"ขั้นที่ 1/2 วิเคราะห์ noise profile {pct:.0f}%"
        return 15 + pct * 0.85, f"ขั้นที่ 2/2 ลด noise {pct:.0f}%"
    return None


def get_duration(path):
    try:
        out = subprocess.run(
            [C.find_tool("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=180, **C.hide_console()).stdout
        return float(out.strip())
    except Exception:  # noqa: BLE001
        return 0.0


def preflight(engine, values, video_denoise):
    """คืนรายการปัญหาที่พบ (ว่าง = พร้อมทำงาน)"""
    problems = []
    if not os.path.isfile(ENGINE_SCRIPT):
        problems.append("ไม่พบ denoise_video.py ในโฟลเดอร์เดียวกับโปรแกรมนี้")
    if engine == "spectral" and not os.path.isfile(os.path.join(HERE, "denoise_spectral.py")):
        problems.append("ไม่พบ denoise_spectral.py ในโฟลเดอร์เดียวกับโปรแกรมนี้")
    return problems + C.engine_problems(engine, values, video_denoise)


def kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True, **C.hide_console())
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass


class Job(threading.Thread):
    """รันงานเบื้องหลัง ส่งเหตุการณ์เข้า queue: log / progress / done"""

    def __init__(self, opts, src, dst, events):
        super().__init__(daemon=True)
        self.o, self.src, self.dst, self.q = opts, src, dst, events
        self.proc = None
        self.cancelled = False
        self.tmp_dir = None

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            kill_tree(self.proc)

    def _cleanup_partial(self):
        if os.path.exists(self.dst):
            try:
                os.remove(self.dst)
            except OSError:
                pass

    def run(self):
        tail, rc = [], -1
        try:
            src = self.src
            if self.o.get("preview"):
                self.tmp_dir = tempfile.mkdtemp(prefix="denoise_")
                clip = os.path.join(self.tmp_dir, "clip" + os.path.splitext(src)[1])
                self.q.put(("log", f"ตัดตัวอย่าง {PREVIEW_SECONDS // 60} นาทีแรก..."))
                r = subprocess.run(
                    [C.find_tool("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-nostdin",
                     "-t", str(PREVIEW_SECONDS), "-i", src,
                     "-map", "0:v:0?", "-map", "0:a:0", "-c", "copy", clip],
                    capture_output=True, **C.hide_console())
                if r.returncode != 0:
                    raise RuntimeError("ตัดตัวอย่างไม่สำเร็จ: " + r.stderr.decode("utf-8", "replace"))
                src = clip

            duration = get_duration(src)
            cmd = build_command(self.o, src, self.dst)
            self.q.put(("log", "เริ่มประมวลผล..."))
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            kw = C.hide_console()
            if os.name != "nt":
                kw["start_new_session"] = True
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, env=env, **kw)

            def handle(raw):
                nonlocal tail
                line = raw.decode("utf-8", "replace")
                prog = parse_progress(line)
                if prog:
                    self.q.put(("progress", prog[0], prog[1]))
                elif line.strip():
                    tail.append(line.strip())
                    tail = tail[-15:]
                    self.q.put(("log", line.strip()))

            pending = b""
            while True:
                chunk = self.proc.stderr.read1(4096)
                if not chunk:
                    break
                parts = re.split(rb"[\r\n]+", pending + chunk)
                pending = parts.pop()
                for seg in parts:
                    if seg:
                        handle(seg)
            if pending:
                handle(pending)
            rc = self.proc.wait()
        except Exception as e:  # noqa: BLE001
            tail.append(str(e))
        finally:
            if self.tmp_dir:
                shutil.rmtree(self.tmp_dir, ignore_errors=True)
        if self.cancelled:
            self._cleanup_partial()
            self.q.put(("done", "cancelled", "ยกเลิกแล้ว"))
        elif rc == 0:
            self.q.put(("done", "ok", self.dst))
        else:
            self._cleanup_partial()
            self.q.put(("done", "error", "\n".join(tail[-8:]) or f"รหัสข้อผิดพลาด {rc}"))


# ============================================================ GUI
def _drop_title_icon_slot(root):
    """ตัดช่องว่างของไอคอนที่แถบชื่อ ให้ข้อความชิดซ้าย (Windows)"""
    try:
        import ctypes
        u = ctypes.windll.user32
        root.update_idletasks()
        hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
        ex = u.GetWindowLongW(hwnd, -20)           # GWL_EXSTYLE
        u.SetWindowLongW(hwnd, -20, ex | 0x0001)   # WS_EX_DLGMODALFRAME
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
    except Exception:  # noqa: BLE001
        pass


def set_app_icon(root, tk):
    """ตั้งค่าไอคอนโปรแกรมสำหรับ Title bar และ Taskbar ด้วย icon.ico ของโปรเจกต์"""
    try:
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        candidates = [os.path.join(base_dir, "icon.ico"),
                      os.path.join(base_dir, "icons", "icon.ico"),
                      os.path.join(HERE, "icons", "icon.ico")]
        ico_path = next((p for p in candidates if os.path.isfile(p)), "")

        if os.name == "nt":
            import ctypes
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("nosieprogram.denoise.gui")
            except Exception:
                pass
            if os.path.isfile(ico_path):
                root.iconbitmap(default=ico_path)
                return
        elif os.path.isfile(ico_path):
            try:
                from PIL import Image, ImageTk
                img = ImageTk.PhotoImage(Image.open(ico_path))
                root.iconphoto(True, img)
                return
            except Exception:
                pass
    except Exception:  # noqa: BLE001
        pass


def open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def enable_high_dpi():
    """เปิดใช้งาน High-DPI Awareness บน Windows ให้ UI และฟอนต์คมชัดระดับพิกเซลแท้ (ไม่เบลอจาก Bitmap scaling)"""
    if os.name == "nt":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware v2
            except Exception:
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI Aware
                except Exception:
                    ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def configure_sharp_ui(root):
    """ปรับแต่งฟอนต์ สไตล์ และธีมของระบบให้คมชัดระดับ Subpixel Typography"""
    import tkinter.font as tkfont
    from tkinter import ttk

    available = set(tkfont.families(root))
    if "Leelawadee UI" in available:
        ui_font = "Leelawadee UI"
    elif "Segoe UI" in available:
        ui_font = "Segoe UI"
    else:
        ui_font = "Tahoma"

    mono_font = "Consolas" if "Consolas" in available else "Courier New"

    # ปรับ Named Fonts ทั้งหมดของ Tkinter
    for name, sz, wt in (
        ("TkDefaultFont", 10, "normal"),
        ("TkTextFont", 10, "normal"),
        ("TkMenuFont", 10, "normal"),
        ("TkHeadingFont", 10, "bold"),
        ("TkCaptionFont", 9, "normal"),
        ("TkSmallCaptionFont", 9, "normal"),
        ("TkIconFont", 10, "normal"),
        ("TkTooltipFont", 9, "normal"),
    ):
        try:
            f = tkfont.nametofont(name)
            f.configure(family=ui_font, size=sz, weight=wt)
        except Exception:
            pass

    try:
        f = tkfont.nametofont("TkFixedFont")
        f.configure(family=mono_font, size=10)
    except Exception:
        pass

    # ใช้ clam เป็นฐานเพื่อให้ปรับสีธีมได้สม่ำเสมอทั้ง Windows และ Linux
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure("TLabelframe", padding=(10, 6))
    style.configure("TLabelframe.Label", font=(ui_font, 10, "bold"))
    style.configure("TButton", font=(ui_font, 10), padding=(10, 4))
    style.configure("TLabel", font=(ui_font, 10))
    style.configure("TRadiobutton", font=(ui_font, 10))
    style.configure("TCheckbutton", font=(ui_font, 10))
    style.configure("TEntry", font=(ui_font, 10), padding=(6, 4))
    style.configure("TCombobox", font=(ui_font, 10), padding=(6, 4))
    style.configure("TProgressbar", thickness=15)

    return ui_font, mono_font


THEMES = {
    "light": {"bg": "#ffffff", "surface": "#ffffff", "text": "#172033",
              "muted": "#5b6575", "border": "#cbd5e1", "entry": "#ffffff",
              "log": "#ffffff", "accent": "#2563eb", "select": "#bfdbfe",
              "trough": "#e5e7eb", "mark": "#000000", "scroll_thumb": "#64748b"},
    "dark": {"bg": "#111827", "surface": "#1f2937", "text": "#e5e7eb",
             "muted": "#aab4c4", "border": "#374151", "entry": "#111827",
             "log": "#0b1220", "accent": "#60a5fa", "select": "#1e3a5f",
             "trough": "#374151", "mark": "#ffffff", "scroll_thumb": "#d1d5db"},
}


def make_folder_icon(root, color):
    """Draw a small folder icon using the current theme accent color."""
    import tkinter as tk

    icon = tk.PhotoImage(master=root, width=20, height=18)
    pixels = set()
    for x in range(4, 10):
        pixels.add((x, 3))
    for y in range(4, 16):
        for x in range(2, 18):
            pixels.add((x, y))
    for x in range(2, 18):
        pixels.add((x, 5))
    for x, y in pixels:
        icon.put(color, to=(x, y))
    return icon


def draw_check_indicator(canvas, checked, palette):
    """Draw a large, crisp check instead of the platform-dependent X marker."""
    canvas.delete("all")
    canvas.configure(background=palette["bg"])
    canvas.create_rectangle(2, 2, 24, 24, fill=palette["surface"],
                            outline=palette["border"], width=2)
    if checked:
        canvas.create_line(7, 13, 11, 18, 20, 8, fill=palette["mark"],
                           width=2, capstyle="round", joinstyle="round")


def apply_windows_chrome(root, theme_name, palette):
    """Match the native Windows title bar and resize border to the selected theme."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        client_hwnd = wintypes.HWND(root.winfo_id())
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        hwnd = user32.GetAncestor(client_hwnd, 2) or client_hwnd  # GA_ROOT
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                               wintypes.LPCVOID, wintypes.DWORD]
        # HRESULT is a signed 32-bit value; ctypes.wintypes does not expose it on all Python builds.
        dwm.DwmSetWindowAttribute.restype = ctypes.c_int32

        def set_attribute(target, attribute, value):
            result = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(target, attribute, ctypes.byref(result),
                                             ctypes.sizeof(result))

        dark = theme_name == "dark"

        def colorref(hex_color):
            rgb = bytes.fromhex(hex_color.lstrip("#"))
            return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)

        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        # Attribute 20 is current Windows 11; 19 supports earlier builds.
        if set_attribute(hwnd, 20, int(dark)) < 0:
            set_attribute(hwnd, 19, int(dark))
        set_attribute(hwnd, 35, colorref(palette["bg"]))       # DWMWA_CAPTION_COLOR
        set_attribute(hwnd, 36, colorref(palette["text"]))     # DWMWA_TEXT_COLOR
        set_attribute(hwnd, 34, colorref(palette["border"]))   # DWMWA_BORDER_COLOR
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x27)
        dwm.DwmFlush()
    except Exception:
        # Older Windows versions keep the system title bar if DWM colors are unavailable.
        pass


def apply_ui_theme(root, canvas, content, theme_name, logbox=None, sliders=(),
                   theme_button=None, check_canvas=None, check_var=None, folder_button=None,
                   scrollbar_control=None):
    """Apply one palette to ttk, the scroll surface, and classic Tk controls."""
    from tkinter import ttk

    theme_name = theme_name if theme_name in THEMES else "light"
    p = THEMES[theme_name]
    style = ttk.Style(root)
    root.configure(background=p["bg"])
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
    style.configure("TLabelframe", background=p["bg"], foreground=p["text"],
                     bordercolor=p["border"])
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["text"])
    style.configure("TButton", background=p["surface"], foreground=p["text"],
                     bordercolor=p["border"])
    style.map("TButton", background=[("active", p["select"]), ("disabled", p["bg"])],
              foreground=[("disabled", p["muted"])])
    style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
    style.map("TCheckbutton", background=[("active", p["bg"])])
    style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
    style.map("TRadiobutton", background=[("active", p["bg"])])
    style.configure("TEntry", fieldbackground=p["entry"], foreground=p["text"])
    style.configure("TSpinbox", fieldbackground=p["entry"], foreground=p["text"])
    style.configure("TCombobox", fieldbackground=p["entry"], foreground=p["text"],
                     background=p["surface"])
    style.map("TCombobox", fieldbackground=[("readonly", p["entry"])],
              foreground=[("readonly", p["text"])])
    style.configure("TProgressbar", background=p["accent"], troughcolor=p["trough"])
    style.configure("Vertical.TScrollbar", background=p["surface"],
                     troughcolor=p["bg"], bordercolor=p["bg"], arrowcolor=p["text"])
    canvas.configure(background=p["bg"])
    content.configure(style="TFrame")
    for slider in sliders:
        slider.configure(bg=p["bg"], fg=p["text"], troughcolor=p["trough"],
                         activebackground=p["accent"], highlightbackground=p["bg"])
    if logbox is not None:
        logbox.configure(bg=p["log"], fg=p["text"], insertbackground=p["text"],
                         selectbackground=p["select"], selectforeground=p["text"])
    if theme_button is not None:
        theme_button.configure(text="ธีมสว่าง" if theme_name == "light" else "ธีมมืด")
    if check_canvas is not None and check_var is not None:
        draw_check_indicator(check_canvas, check_var.get(), p)
    if folder_button is not None:
        icon = make_folder_icon(root, p["mark"])
        folder_button.configure(image=icon, compound="left")
        folder_button._icon_image = icon
    if scrollbar_control is not None:
        scrollbar_control.set_palette(p)
    apply_windows_chrome(root, theme_name, p)


def main():
    if sys.version_info < C.MIN_PYTHON:
        _fatal("ต้องใช้ Python %d.%d ขึ้นไป" % C.MIN_PYTHON)
    enable_high_dpi()
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    class ShortThumbScrollbar(tk.Canvas):
        """Full-height scrollbar track with a deliberately shorter draggable thumb."""

        def __init__(self, master, command, palette):
            super().__init__(master, width=20, highlightthickness=0, borderwidth=0,
                             takefocus=False, cursor="arrow", background=palette["bg"])
            self.command = command
            self.palette = palette
            self.first, self.last = 0.0, 1.0
            self.thumb_top = 0.0
            self.thumb_height = 0.0
            self.track_top = 5.0
            self.track_bottom = 5.0
            self.drag_offset = None
            self.bind("<Configure>", self._draw)
            self.bind("<Button-1>", self._press)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._release)
            self.bind("<space>", lambda _event: self.command("scroll", 1, "pages"))

        def set_palette(self, palette):
            self.palette = palette
            self._draw()

        def set(self, first, last):
            self.first = min(1.0, max(0.0, float(first)))
            self.last = min(1.0, max(self.first, float(last)))
            self._draw()

        def _draw(self, _event=None):
            self.delete("all")
            width, height = self.winfo_width(), self.winfo_height()
            self.configure(background=self.palette["bg"])
            if width <= 2 or height <= 2:
                return

            self.track_top = 16.0
            self.track_bottom = max(self.track_top + 1, height - 16.0)
            track_length = self.track_bottom - self.track_top
            visible = max(0.0, min(1.0, self.last - self.first))
            scroll_range = max(0.0, 1.0 - visible)
            if scroll_range <= 1e-6:
                self.thumb_height = track_length
            else:
                # Shorten only the handle, while retaining the full-height track.
                self.thumb_height = max(48.0, track_length * visible * 0.68)
                self.thumb_height = min(track_length, self.thumb_height)
            travel = max(0.0, track_length - self.thumb_height)
            position = self.first / scroll_range if scroll_range > 1e-6 else 0.0
            self.thumb_top = self.track_top + travel * min(1.0, max(0.0, position))

            mid = width / 2.0
            self.create_polygon(mid, 4, mid - 4, 10, mid + 4, 10,
                                fill=self.palette["scroll_thumb"], outline="")
            self.create_polygon(mid, height - 4, mid - 4, height - 10,
                                mid + 4, height - 10,
                                fill=self.palette["scroll_thumb"], outline="")
            self.create_line(mid, self.track_top, mid, self.track_bottom,
                             fill=self.palette["trough"], width=2, tags="track")
            inset = 3.0
            self.create_rectangle(inset, self.thumb_top, width - inset,
                                  self.thumb_top + self.thumb_height,
                                  fill=self.palette["scroll_thumb"],
                                  outline=self.palette["border"], width=1, tags="thumb")

        def _press(self, event):
            if event.y < self.track_top:
                self.command("scroll", -1, "units")
                return "break"
            if event.y > self.track_bottom:
                self.command("scroll", 1, "units")
                return "break"
            if self.thumb_top <= event.y <= self.thumb_top + self.thumb_height:
                self.drag_offset = event.y - self.thumb_top
            else:
                direction = -1 if event.y < self.thumb_top else 1
                self.command("scroll", direction, "pages")
                self.drag_offset = self.thumb_height / 2.0
            return "break"

        def _drag(self, event):
            if self.drag_offset is None:
                return "break"
            travel = max(1.0, self.track_bottom - self.track_top - self.thumb_height)
            top = min(self.track_bottom - self.thumb_height,
                      max(self.track_top, event.y - self.drag_offset))
            position = (top - self.track_top) / travel
            visible = max(0.0, min(1.0, self.last - self.first))
            self.command("moveto", position * (1.0 - visible))
            return "break"

        def _release(self, _event):
            self.drag_offset = None

    cfg = C.load_settings()
    initial_theme = cfg.get("theme", "light")
    initial_palette = THEMES.get(initial_theme, THEMES["light"])

    root = tk.Tk()
    root.withdraw()
    root.title(APP_TITLE)
    root.configure(background=initial_palette["bg"])
    set_app_icon(root, tk)
    ui_font, mono_font = configure_sharp_ui(root)
    root.geometry("1000x820")
    root.minsize(680, 520)

    # Keep the controls usable on short windows while letting the content width
    # follow the window as it is resized.
    scroll_frame = ttk.Frame(root)
    scroll_frame.pack(fill="both", expand=True)
    scroll_frame.columnconfigure(0, weight=1)
    scroll_frame.rowconfigure(0, weight=1)
    canvas = tk.Canvas(scroll_frame, highlightthickness=0, borderwidth=0,
                       background=initial_palette["bg"])
    scrollbar = ShortThumbScrollbar(scroll_frame, command=canvas.yview,
                                    palette=initial_palette)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")
    content = ttk.Frame(canvas)
    content_id = canvas.create_window((0, 0), window=content, anchor="nw")
    scroll_cache = {"width": None, "region": None, "pending_width": None,
                    "width_job": None}

    def update_scroll_region(_event=None):
        region = canvas.bbox("all")
        if region and region != scroll_cache["region"]:
            canvas.configure(scrollregion=region)
            scroll_cache["region"] = region

    def fit_content_width(event):
        # Minimize/restore can briefly report a zero-width client area. Ignore
        # it, and coalesce resize events so the embedded frame is laid out once.
        if event.width <= 1:
            return
        scroll_cache["pending_width"] = event.width
        if scroll_cache["width_job"] is not None:
            root.after_cancel(scroll_cache["width_job"])
        scroll_cache["width_job"] = root.after(90, apply_content_width)

    def apply_content_width():
        scroll_cache["width_job"] = None
        width = scroll_cache["pending_width"]
        if width is not None and width != scroll_cache["width"]:
            canvas.itemconfigure(content_id, width=width)
            scroll_cache["width"] = width

    content.bind("<Configure>", update_scroll_region)
    canvas.bind("<Configure>", fit_content_width)

    def scroll_content(event):
        try:
            if event.widget.winfo_class() in ("Text", "Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox"):
                return
        except Exception:
            pass
        if getattr(event, "num", None) == 4:
            canvas.yview_scroll(-3, "units")
        elif getattr(event, "num", None) == 5:
            canvas.yview_scroll(3, "units")
        else:
            steps = int(-event.delta / 120)
            if steps == 0 and event.delta:
                steps = -1 if event.delta > 0 else 1
            canvas.yview_scroll(steps, "units")

    root.bind_all("<MouseWheel>", scroll_content)
    root.bind_all("<Button-4>", scroll_content)
    root.bind_all("<Button-5>", scroll_content)

    selection_focus = {"widget": None}
    text_selection_classes = {"Entry", "TEntry", "TCombobox", "Text"}

    def resolve_tk_widget(candidate):
        if candidate is None:
            return None
        if isinstance(candidate, str):
            try:
                candidate = root.nametowidget(candidate)
            except (KeyError, tk.TclError):
                return None
        return candidate if callable(getattr(candidate, "winfo_class", None)) else None

    def remember_selection_focus(event):
        widget = resolve_tk_widget(getattr(event, "widget", None))
        try:
            selection_focus["widget"] = (
                widget if widget is not None and
                widget.winfo_class() in text_selection_classes else None)
        except tk.TclError:
            selection_focus["widget"] = None

    def clear_widget_selection(candidate):
        widget = resolve_tk_widget(candidate)
        if widget is None:
            return
        try:
            if hasattr(widget, "selection_clear"):
                widget.selection_clear()
            if widget.winfo_class() == "Text":
                widget.tag_remove("sel", "1.0", "end")
        except tk.TclError:
            pass

    def clear_text_selection(event):
        # Clear both the text highlight and its active focus when clicking
        # outside that field, without changing the field's value.
        previous = resolve_tk_widget(selection_focus["widget"])
        target = resolve_tk_widget(getattr(event, "widget", None))
        if previous is not None:
            clear_widget_selection(previous)
        if target is not None:
            clear_widget_selection(target)
        try:
            focused = resolve_tk_widget(root.focus_get())
            if (previous is not None and
                    (target is None or previous._w != target._w) and
                    focused is not None and focused._w == previous._w):
                # Non-focusable targets leave the old entry active; move focus
                # to the window so the old control loses its active styling.
                root.focus_set()
            focused = resolve_tk_widget(root.focus_get())
            selection_focus["widget"] = (
                focused if focused is not None and
                focused.winfo_class() in text_selection_classes else None)
        except tk.TclError:
            selection_focus["widget"] = None

    root.bind_all("<FocusIn>", remember_selection_focus, add="+")
    root.bind_all("<Button-1>", clear_text_selection, add="+")
    pad = {"padx": 10, "pady": 4}

    events = queue.Queue()
    state = {"job": None, "t0": 0.0, "out": "", "user_presets": set(), "rec_path": "",
             "theme": cfg.get("theme", "light")}
    presets = {}

    v_in, v_out = tk.StringVar(), tk.StringVar()
    v_out_dir = tk.StringVar(value=cfg.get("output_dir") or "")
    v_info = tk.StringVar(value="ยังไม่ได้เลือกไฟล์")
    v_preset = tk.StringVar(value=cfg["preset"])
    v_engine = tk.StringVar(value=cfg["engine"])
    v_vals = {k: tk.DoubleVar(value=cfg["values"][k]) for k in C.OPTIONS}
    v_manual = tk.BooleanVar(value=cfg["noise_manual"])
    v_ns, v_nd = tk.DoubleVar(value=cfg["noise_start"]), tk.DoubleVar(value=cfg["noise_dur"])
    v_vid, v_prev = tk.BooleanVar(value=cfg["video_denoise"]), tk.BooleanVar(value=cfg["preview"])
    v_auto_enhance = tk.BooleanVar(value=cfg.get("auto_enhance", True))
    v_wind_reduction = tk.BooleanVar(value=cfg.get("wind_reduction", True))
    v_auto_tune = tk.BooleanVar(value=cfg.get("auto_tune", True))
    v_br = tk.StringVar(value=cfg["bitrate"])
    v_status = tk.StringVar(value="พร้อมทำงาน")
    v_time = tk.StringVar(value="")

    # ---- 1. อัดเสียงจากไมโครโฟน
    rec_inst = audio_recorder.AudioRecorder() if audio_recorder else None
    v_rec_status = tk.StringVar(
        value="พร้อมอัดเสียง" if rec_inst and rec_inst.is_available()[0] else "ไมโครโฟนไม่พร้อมใช้งาน")
    v_rec_dur = tk.StringVar(value="00:00")

    theme_button = ttk.Button(content, width=15, takefocus=False)
    theme_button.pack(anchor="e", padx=10, pady=(0, 2))
    f_rec = ttk.LabelFrame(content, text="1. เริ่มอัดเสียงจากไมโครโฟน")
    f_rec.pack(fill="x", **pad)
    f_rec.columnconfigure(2, weight=1)

    r_row1 = ttk.Frame(f_rec)
    r_row1.pack(fill="x", padx=6, pady=3)

    b_rec_start = ttk.Button(r_row1, text="●  เริ่มอัดเสียง", width=13)
    b_rec_start.pack(side="left", padx=(0, 4))
    b_rec_stop = ttk.Button(r_row1, text="■  หยุด", width=8, state="disabled")
    b_rec_stop.pack(side="left", padx=4)
    b_rec_play = ttk.Button(r_row1, text="▶  ฟังเสียงที่อัด", width=13, state="disabled")
    b_rec_play.pack(side="left", padx=4)
    b_rec_use = ttk.Button(r_row1, text="นำเข้าไฟล์เพื่อลดเสียงรบกวน", width=22, state="disabled")
    b_rec_use.pack(side="left", padx=4)
    b_rec_save = ttk.Button(r_row1, text="บันทึกเสียงจากไมค์", width=16, state="disabled")
    b_rec_save.pack(side="left", padx=4)

    r_row2 = ttk.Frame(f_rec)
    r_row2.pack(fill="x", padx=6, pady=(0, 4))
    v_rec_autogain = tk.BooleanVar(value=True)
    autogain_box = ttk.Frame(r_row2)
    autogain_box.pack(side="left", padx=(4, 10))
    check_canvas = tk.Canvas(autogain_box, width=26, height=26, highlightthickness=0,
                             borderwidth=0, takefocus=True, cursor="hand2")
    check_canvas.pack(side="left", padx=(0, 4))
    autogain_label = ttk.Label(autogain_box, text="เพิ่มระดับเสียงไมค์อัตโนมัติ",
                               cursor="hand2")
    autogain_label.pack(side="left")

    def toggle_autogain(_event=None):
        v_rec_autogain.set(not v_rec_autogain.get())
        draw_check_indicator(check_canvas, v_rec_autogain.get(), THEMES[state["theme"]])
        return "break"

    check_canvas.bind("<Button-1>", toggle_autogain)
    autogain_label.bind("<Button-1>", toggle_autogain)
    check_canvas.bind("<space>", toggle_autogain)
    ttk.Label(r_row2, text="ระดับเสียงไมค์:").pack(side="left")
    rec_meter = ttk.Progressbar(r_row2, maximum=100, length=120)
    rec_meter.pack(side="left", padx=6)
    ttk.Label(r_row2, text="เวลาอัด:").pack(side="left", padx=(6, 2))
    ttk.Label(r_row2, textvariable=v_rec_dur, font=("TkDefaultFont", 9, "bold")).pack(side="left")
    ttk.Label(r_row2, text=" | ").pack(side="left", padx=4)
    ttk.Label(r_row2, textvariable=v_rec_status, style="Muted.TLabel").pack(side="left")

    def update_rec_ui():
        if rec_inst and rec_inst.is_recording:
            dur = rec_inst.get_duration()
            mins, secs = divmod(int(dur), 60)
            duration_text = f"{mins:02d}:{secs:02d}"
            if v_rec_dur.get() != duration_text:
                v_rec_dur.set(duration_text)
            peak, _ = rec_inst.get_levels()
            meter_value = min(100, int(peak * 250))
            if rec_meter["value"] != meter_value:
                rec_meter["value"] = meter_value
            root.after(150, update_rec_ui)
        else:
            rec_meter["value"] = 0

    def on_rec_start():
        if not rec_inst:
            return messagebox.showerror("ข้อผิดพลาด", "ไม่พบโมดูลอัดเสียง sounddevice")
        ok, msg = rec_inst.start_recording()
        if not ok:
            return messagebox.showerror("เปิดไมค์ไม่ได้", msg)
        b_rec_start.config(state="disabled")
        b_rec_stop.config(state="normal")
        b_rec_play.config(state="disabled")
        b_rec_use.config(state="disabled")
        b_rec_save.config(state="disabled")
        v_rec_status.set("กำลังบันทึกเสียง...")
        v_rec_dur.set("00:00")
        root.after(100, update_rec_ui)

    def on_rec_stop():
        if not rec_inst:
            return
        ok, msg = rec_inst.stop_recording()
        b_rec_start.config(state="normal")
        b_rec_stop.config(state="disabled")
        if not ok:
            v_rec_status.set(msg)
            return messagebox.showwarning("แจ้งเตือน", msg)
        v_rec_status.set(msg)
        # บันทึกลงไฟล์ชั่วคราว (พร้อม Auto Gain หากเลือกไว้)
        tmp_wav = os.path.join(tempfile.gettempdir(), f"rec_{int(time.time())}.wav")
        saved, smsg = rec_inst.save_to_file(tmp_wav, normalize=v_rec_autogain.get())
        if saved:
            state["rec_path"] = tmp_wav
            b_rec_play.config(state="normal")
            b_rec_use.config(state="normal")
            b_rec_save.config(state="normal")

    def on_rec_play():
        p = state.get("rec_path")
        if p and os.path.isfile(p):
            open_path(p)

    def on_rec_use():
        p = state.get("rec_path")
        if not p or not os.path.isfile(p):
            return
        v_in.set(p)
        v_out.set(default_output(p, v_prev.get(), v_out_dir.get()))
        v_info.set(f"เสียงที่อัดสด ({v_rec_dur.get()} นาที)")
        threading.Thread(
            target=lambda path=p: events.put(("info", path, get_duration(path))),
            daemon=True).start()
        messagebox.showinfo("นำเข้าสำเร็จ", "นำเข้าเสียงที่บันทึกเป็นไฟล์ต้นฉบับเรียบร้อยแล้ว\nคุณสามารถกด 'เริ่มลด noise' ได้ทันที")

    def on_rec_save():
        p = state.get("rec_path")
        if not p or not os.path.isfile(p):
            return
        dest = filedialog.asksaveasfilename(
            title="บันทึกเสียงดิบเป็นไฟล์",
            initialfile=f"voice_record_{time.strftime('%Y%m%d_%H%M%S')}.wav",
            initialdir=v_out_dir.get() or cfg["last_dir"] or None,
            filetypes=[("WAV Audio", "*.wav")])
        if dest:
            shutil.copy2(p, dest)
            messagebox.showinfo("บันทึกแล้ว", f"บันทึกไฟล์เสียงสำเร็จ:\n{dest}")

    b_rec_start.config(command=on_rec_start)
    b_rec_stop.config(command=on_rec_stop)
    b_rec_play.config(command=on_rec_play)
    b_rec_use.config(command=on_rec_use)
    b_rec_save.config(command=on_rec_save)

    # ---- 2. เลือกไฟล์และโฟลเดอร์บันทึก
    f = ttk.LabelFrame(content, text="2. เลือกไฟล์และโฟลเดอร์บันทึก")
    f.pack(fill="x", **pad)
    f.columnconfigure(1, weight=1)
    ttk.Label(f, text="ไฟล์ต้นฉบับ").grid(row=0, column=0, sticky="w", padx=6, pady=3)
    ttk.Entry(f, textvariable=v_in).grid(row=0, column=1, sticky="ew", padx=6)
    
    ttk.Label(f, text="โฟลเดอร์บันทึก").grid(row=1, column=0, sticky="w", padx=6, pady=3)
    ttk.Entry(f, textvariable=v_out_dir).grid(row=1, column=1, sticky="ew", padx=6)

    ttk.Label(f, text="บันทึกไฟล์เป็น").grid(row=2, column=0, sticky="w", padx=6, pady=3)
    ttk.Entry(f, textvariable=v_out).grid(row=2, column=1, sticky="ew", padx=6)
    ttk.Label(f, textvariable=v_info, style="Muted.TLabel").grid(
        row=3, column=1, sticky="w", padx=6, pady=(0, 4))

    def pick_in():
        p = filedialog.askopenfilename(
            title="เลือกไฟล์เสียงหรือวิดีโอ", initialdir=cfg["last_dir"] or None,
            filetypes=[("วิดีโอและเสียงทั้งหมด", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.flv "
                                             "*.mp3 *.wav *.m4a *.flac *.ogg *.aac"),
                       ("ไฟล์เสียง", "*.wav *.mp3 *.m4a *.flac *.ogg *.aac"),
                       ("ไฟล์วิดีโอ", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.flv"),
                       ("ทุกไฟล์", "*.*")])
        if p:
            cfg["last_dir"] = os.path.dirname(p)
            v_in.set(p)
            v_out.set(default_output(p, v_prev.get(), v_out_dir.get()))
            v_info.set("กำลังอ่านข้อมูลไฟล์...")
            threading.Thread(
                target=lambda path=p: events.put(("info", path, get_duration(path))),
                daemon=True).start()

    def pick_dir():
        d = filedialog.askdirectory(
            title="เลือกโฟลเดอร์ที่จะบันทึกไฟล์ผลลัพธ์",
            initialdir=v_out_dir.get() or cfg["last_dir"] or None)
        if d:
            v_out_dir.set(d)
            if v_in.get():
                v_out.set(default_output(v_in.get(), v_prev.get(), d))

    def pick_out():
        init_dir = v_out_dir.get() or cfg["last_dir"] or (os.path.dirname(v_out.get()) if v_out.get() else None)
        p = filedialog.asksaveasfilename(
            title="ระบุชื่อไฟล์และตำแหน่งที่จะบันทึก",
            initialfile=os.path.basename(v_out.get()) if v_out.get() else "output_denoised.wav",
            initialdir=init_dir or None,
            filetypes=[("ไฟล์ผลลัพธ์", "*.wav *.mp3 *.m4a *.mp4 *.flac *.mkv"), ("ทุกไฟล์", "*.*")])
        if p:
            v_out.set(p)
            v_out_dir.set(os.path.dirname(p))

    ttk.Button(f, text="เลือกไฟล์...", command=pick_in, width=16).grid(row=0, column=2, padx=6)
    ttk.Button(f, text="เลือกโฟลเดอร์...", command=pick_dir, width=16).grid(row=1, column=2, padx=6)
    ttk.Button(f, text="ระบุชื่อ...", command=pick_out, width=16).grid(row=2, column=2, padx=6)

    # ---- 3. ตั้งค่า (สร้างจากรายการใน denoise_common)
    s = ttk.LabelFrame(content, text="3. ตั้งค่าการลด noise")
    s.pack(fill="x", **pad)
    s.columnconfigure(1, weight=1)

    ttk.Label(s, text="เครื่องมือ").grid(row=0, column=0, sticky="w", padx=6, pady=3)
    er = ttk.Frame(s)
    er.grid(row=0, column=1, columnspan=2, sticky="w", padx=6)
    radios = {}
    for eid, spec in C.ENGINES.items():
        rb = ttk.Radiobutton(er, text=spec["label"], variable=v_engine, value=eid)
        rb.pack(side="left", padx=(0, 12))
        radios[eid] = rb

    ttk.Label(s, text="พรีเซ็ต").grid(row=1, column=0, sticky="w", padx=6, pady=3)
    cb = ttk.Combobox(s, textvariable=v_preset, state="readonly")
    cb.grid(row=1, column=1, sticky="ew", padx=6)
    pb = ttk.Frame(s)
    pb.grid(row=1, column=2, sticky="w", padx=6)
    b_psave = ttk.Button(pb, text="บันทึก...", width=9)
    b_psave.pack(side="left")
    b_pdel = ttk.Button(pb, text="ลบ", width=5)
    b_pdel.pack(side="left", padx=(4, 0))

    def refresh_presets():
        builtin, user = C.load_presets()
        presets.clear()
        presets.update(builtin)
        presets.update(user)
        state["user_presets"] = set(user)
        cb["values"] = list(presets)
        if v_preset.get() not in presets:
            v_preset.set(next(iter(presets)))
        b_pdel.config(state="normal" if v_preset.get() in state["user_presets"] else "disabled")

    def apply_preset(*_):
        for k, v in presets.get(v_preset.get(), {}).items():
            v_vals[k].set(v)
        b_pdel.config(state="normal" if v_preset.get() in state["user_presets"] else "disabled")

    cb.bind("<<ComboboxSelected>>", apply_preset)

    def save_preset():
        name = simpledialog.askstring("บันทึกพรีเซ็ต", "ตั้งชื่อพรีเซ็ตจากค่าปัจจุบัน:", parent=root)
        if not name or not name.strip():
            return
        name = name.strip()[:60]
        builtin, user = C.load_presets()
        if name in builtin:
            return messagebox.showwarning("ชื่อซ้ำ", "ชื่อนี้ใช้โดยพรีเซ็ตในตัวแล้ว กรุณาใช้ชื่ออื่น")
        if name in user and not messagebox.askyesno("มีอยู่แล้ว", f"เขียนทับพรีเซ็ต “{name}” หรือไม่?"):
            return
        user[name] = C.clean_values({k: v.get() for k, v in v_vals.items()})
        if not C.save_user_presets(user):
            return messagebox.showerror("บันทึกไม่ได้", f"เขียนไฟล์ไม่สำเร็จ:\n{C.config_dir()}")
        v_preset.set(name)
        refresh_presets()

    def delete_preset():
        name = v_preset.get()
        if name not in state["user_presets"]:
            return
        if not messagebox.askyesno("ลบพรีเซ็ต", f"ลบพรีเซ็ต “{name}” หรือไม่?"):
            return
        _, user = C.load_presets()
        user.pop(name, None)
        C.save_user_presets(user)
        v_preset.set(next(iter(C.BUILTIN_PRESETS)))
        refresh_presets()
        apply_preset()

    b_psave.config(command=save_preset)
    b_pdel.config(command=delete_preset)

    sliders, opt_rows = [], {}
    for i, (key, spec) in enumerate(C.OPTIONS.items()):
        row = 2 + i
        lb = ttk.Label(s, text=spec["label"])
        lb.grid(row=row, column=0, sticky="w", padx=6)
        sc = tk.Scale(s, variable=v_vals[key], from_=spec["min"], to=spec["max"],
                      resolution=spec["step"], orient="horizontal",
                      font=(ui_font, 9), highlightthickness=0, bd=1,
                      sliderrelief="raised", sliderlength=20, activebackground="#3b82f6")
        sc.grid(row=row, column=1, sticky="ew", padx=6)
        ht = ttk.Label(s, text=spec["hint"], style="Muted.TLabel", wraplength=240)
        ht.grid(row=row, column=2, sticky="w", padx=6)
        sliders.append(sc)
        opt_rows[key] = (lb, sc, ht)

    nrow = 2 + len(C.OPTIONS)
    nr = ttk.Frame(s)
    nr.grid(row=nrow, column=0, columnspan=3, sticky="w", padx=6, pady=3)
    ttk.Checkbutton(nr, text="ระบุช่วงที่เป็น noise ล้วน: เริ่มที่วินาที",
                    variable=v_manual).pack(side="left")
    ttk.Spinbox(nr, from_=0, to=100000, width=7, textvariable=v_ns).pack(side="left", padx=4)
    ttk.Label(nr, text="ยาว (วินาที)").pack(side="left")
    ttk.Spinbox(nr, from_=0.2, to=600, increment=0.5, width=5,
                textvariable=v_nd).pack(side="left", padx=4)

    o2 = ttk.Frame(s)
    o2.grid(row=nrow + 1, column=0, columnspan=3, sticky="ew", padx=6, pady=3)
    o2.columnconfigure(0, weight=1)
    ttk.Checkbutton(o2, text="ลด noise ภาพด้วย (ช้าลงมาก)", variable=v_vid).grid(
        row=3, column=0, sticky="w", pady=2)
    b_auto_enhance = ttk.Checkbutton(o2, text="เน้นเสียงพูด ลดเสียงช่วงพัก/ปรับความดังอัตโนมัติ",
                                     variable=v_auto_enhance)
    b_auto_enhance.grid(row=1, column=0, sticky="w", pady=2)
    b_wind_reduction = ttk.Checkbutton(o2, text="ลดลมไมค์ (ตัดเสียงทุ้มต่ำกว่า 150 Hz)",
                                       variable=v_wind_reduction)
    b_wind_reduction.grid(row=2, column=0, sticky="w", pady=2)
    b_auto_tune = ttk.Checkbutton(o2, text="วิเคราะห์เสียงและปรับค่าอัตโนมัติ (Spectral)",
                                  variable=v_auto_tune)
    b_auto_tune.grid(row=0, column=0, sticky="w", pady=2)
    bitrate_row = ttk.Frame(o2)
    bitrate_row.grid(row=4, column=0, sticky="w", pady=2)
    ttk.Label(bitrate_row, text="บิตเรตเสียง").pack(side="left")
    ttk.Combobox(bitrate_row, textvariable=v_br, values=C.BITRATES,
                 width=8, state="readonly").pack(side="left", padx=6)
    ttk.Checkbutton(s, text=f"ทดสอบเฉพาะ {PREVIEW_SECONDS // 60} นาทีแรกก่อน (แนะนำ ใช้ปรับค่าให้เร็ว)",
                    variable=v_prev,
                    command=lambda: v_in.get() and v_out.set(
                        default_output(v_in.get(), v_prev.get(), v_out_dir.get()))
                    ).grid(row=nrow + 2, column=0, columnspan=3, sticky="w", padx=6, pady=3)

    def on_engine_change(*_):
        spec = C.ENGINES.get(v_engine.get(), {})
        for key, widgets in opt_rows.items():
            for w in widgets:
                (w.grid if key in spec.get("options", ()) else w.grid_remove)()
        (nr.grid if spec.get("noise_profile") else nr.grid_remove)()
        b_auto_tune.config(state="normal" if spec.get("noise_profile") else "disabled")

    v_engine.trace_add("write", on_engine_change)

    # ---- 4. ประมวลผล
    r = ttk.LabelFrame(content, text="4. ประมวลผล")
    r.pack(fill="both", expand=True, **pad)
    bar = ttk.Progressbar(r, maximum=100)
    bar.pack(fill="x", padx=6, pady=(6, 2))
    ttk.Label(r, textvariable=v_status).pack(anchor="w", padx=6)
    ttk.Label(r, textvariable=v_time, style="Muted.TLabel").pack(anchor="w", padx=6)
    logbox = tk.Text(r, height=6, state="disabled", wrap="word",
                     font=(mono_font, 9), bg="#f8fafc", fg="#1e293b",
                     relief="solid", bd=1, padx=6, pady=4)
    logbox.pack(fill="both", expand=True, padx=6, pady=4)

    def log(msg):
        logbox.config(state="normal")
        logbox.insert("end", msg + "\n")
        if int(logbox.index("end-1c").split(".")[0]) > 500:
            logbox.delete("1.0", "101.0")
        logbox.see("end")
        logbox.config(state="disabled")

    bf = ttk.Frame(r)
    bf.pack(fill="x", padx=6, pady=(0, 6))
    b_start = ttk.Button(bf, text="▶  เริ่มลด noise")
    b_start.pack(side="left")
    b_cancel = ttk.Button(bf, text="■  ยกเลิก", state="disabled")
    b_cancel.pack(side="left", padx=6)
    b_check = ttk.Button(bf, text="ตรวจระบบ")
    b_check.pack(side="left")
    b_folder = ttk.Button(bf, text="เปิดโฟลเดอร์บันทึก", state="disabled")
    b_folder.pack(side="right")
    b_play = ttk.Button(bf, text="▶  เปิดไฟล์ผลลัพธ์", state="disabled")
    b_play.pack(side="right", padx=6)

    def set_running(running):
        b_start.config(state="disabled" if running else "normal")
        b_cancel.config(state="normal" if running else "disabled")
        cb.config(state="disabled" if running else "readonly")
        for sc in sliders:
            sc.config(state="disabled" if running else "normal")
        for eid, rb in radios.items():
            rb.config(state="disabled" if running else ("normal" if state.get("avail", {}).get(eid, True) else "disabled"))
        b_psave.config(state="disabled" if running else "normal")
        b_auto_enhance.config(state="disabled" if running else "normal")
        b_wind_reduction.config(state="disabled" if running else "normal")
        can_auto_tune = C.ENGINES.get(v_engine.get(), {}).get("noise_profile", False)
        b_auto_tune.config(state="disabled" if running or not can_auto_tune else "normal")

    def collect():
        return {"engine": v_engine.get(),
                "values": {k: v.get() for k, v in v_vals.items()},
                "bitrate": v_br.get(), "noise_manual": v_manual.get(),
                "noise_start": v_ns.get(), "noise_dur": v_nd.get(),
                "video_denoise": v_vid.get(), "preview": v_prev.get(),
                "auto_enhance": v_auto_enhance.get(),
                "wind_reduction": v_wind_reduction.get(),
                "auto_tune": v_auto_tune.get()}

    def store_settings():
        try:
            o = collect()
        except tk.TclError:
            return
        C.save_settings({
            "settings_version": C.SETTINGS_VERSION, "theme": state["theme"],
            "engine": o["engine"],
            "preset": v_preset.get(), "values": o["values"],
            "video_denoise": o["video_denoise"], "preview": o["preview"],
            "auto_enhance": o["auto_enhance"],
            "wind_reduction": o["wind_reduction"],
            "auto_tune": o["auto_tune"],
            "noise_manual": o["noise_manual"], "noise_start": o["noise_start"],
            "noise_dur": o["noise_dur"], "bitrate": o["bitrate"],
            "last_dir": cfg["last_dir"], "output_dir": v_out_dir.get()})

    def start():
        src, dst = v_in.get().strip(), v_out.get().strip()
        if not os.path.isfile(src):
            return messagebox.showwarning("ยังไม่พร้อม", "กรุณาเลือกไฟล์เสียงหรือวิดีโอต้นฉบับ")
        if not dst:
            return messagebox.showwarning("ยังไม่พร้อม", "กรุณาระบุชื่อไฟล์ที่จะบันทึก")
        if os.path.abspath(src) == os.path.abspath(dst):
            return messagebox.showwarning("ชื่อซ้ำ", "ไฟล์ปลายทางต้องไม่ใช่ไฟล์เดียวกับต้นฉบับ")
        try:
            opts = collect()
        except tk.TclError:
            return messagebox.showwarning("ค่าไม่ถูกต้อง", "ช่องวินาทีของช่วง noise ต้องเป็นตัวเลข")
        # สร้างโฟลเดอร์ปลายทางถ้ายังไม่มี
        out_folder = os.path.dirname(os.path.abspath(dst))
        if out_folder and not os.path.exists(out_folder):
            try:
                os.makedirs(out_folder, exist_ok=True)
            except OSError as e:
                return messagebox.showerror("สร้างโฟลเดอร์ไม่ได้", f"ไม่สามารถสร้างโฟลเดอร์บันทึก:\n{e}")
        if os.path.exists(dst) and not messagebox.askyesno(
                "ไฟล์มีอยู่แล้ว", f"{os.path.basename(dst)} มีอยู่แล้ว เขียนทับหรือไม่?"):
            return
        problems = preflight(opts["engine"], opts["values"], opts["video_denoise"])
        if problems:
            return messagebox.showerror("ขาดเครื่องมือที่จำเป็น", "\n".join(problems))
        store_settings()
        logbox.config(state="normal")
        logbox.delete("1.0", "end")
        logbox.config(state="disabled")
        bar["value"] = 0
        v_status.set("กำลังเริ่ม...")
        for b in (b_folder, b_play):
            b.config(state="disabled")
        state["out"], state["t0"] = dst, time.time()
        job = Job(opts, src, dst, events)
        state["job"] = job
        set_running(True)
        job.start()

    def cancel():
        if state["job"] and messagebox.askyesno("ยกเลิก", "ต้องการยกเลิกการประมวลผลหรือไม่?"):
            v_status.set("กำลังยกเลิก...")
            threading.Thread(target=state["job"].cancel, daemon=True).start()

    # ---- ตรวจระบบ
    def run_check():
        v_status.set("กำลังตรวจระบบ...")
        b_check.config(state="disabled")

        def work():
            C.clear_caches()                     # อ่านใหม่ เผื่อเพิ่งติดตั้ง/อัปเดต ffmpeg
            events.put(("report", C.system_report()))
        threading.Thread(target=work, daemon=True).start()

    def show_report(rows):
        b_check.config(state="normal")
        v_status.set("พร้อมทำงาน" if not state["job"] else v_status.get())
        text = C.format_report(rows)
        win = tk.Toplevel(root)
        win.title("ตรวจระบบ")
        win.geometry("760x360")
        win.transient(root)
        colors = THEMES[state["theme"]]
        win.configure(background=colors["bg"])
        box = tk.Text(win, wrap="none", font=("Courier New", 10) if os.name == "nt" else "TkFixedFont",
                      bg=colors["log"], fg=colors["text"], insertbackground=colors["text"])
        box.insert("1.0", text)
        box.config(state="disabled")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        row = ttk.Frame(win)
        row.pack(fill="x", padx=8, pady=(0, 8))

        def copy():
            root.clipboard_clear()
            root.clipboard_append(text)
        ttk.Button(row, text="คัดลอก", command=copy).pack(side="left")
        ttk.Button(row, text="ปิด", command=win.destroy).pack(side="right")
        apply_availability()

    def apply_availability():
        avail = {eid: not C.engine_problems(eid) for eid in C.ENGINES}
        state["avail"] = avail
        for eid, rb in radios.items():
            rb.config(text=C.ENGINES[eid]["label"] + ("" if avail[eid] else "  (ไม่พร้อม)"),
                      state="normal" if avail[eid] else "disabled")
        if not avail.get(v_engine.get(), False):
            ok = [e for e, good in avail.items() if good]
            if ok:
                v_engine.set(ok[0])
        if not any(avail.values()) and not state["job"]:
            v_status.set("ยังใช้งานไม่ได้ — กดปุ่ม “ตรวจระบบ” เพื่อดูสิ่งที่ขาด")

    # ---- คิวเหตุการณ์จาก thread เบื้องหลัง (poll ตลอดเวลา)
    def poll():
        last = None
        try:
            for _ in range(200):          # จำกัดต่อรอบ เพื่อให้หน้าจอได้วาดใหม่เสมอ
                ev = events.get_nowait()
                if ev[0] == "log":
                    log(ev[1])
                elif ev[0] == "progress":
                    last = ev              # เก็บเฉพาะความคืบหน้าล่าสุด
                elif ev[0] == "info":
                    if v_in.get() == ev[1]:
                        v_info.set(f"ความยาว {ev[2] / 60:.1f} นาที" if ev[2]
                                   else "อ่านความยาวไม่ได้")
                elif ev[0] == "caps":
                    apply_availability()
                elif ev[0] == "report":
                    show_report(ev[1])
                elif ev[0] == "done":
                    if last:
                        bar["value"] = last[1]
                    last = None
                    finish(ev[1], ev[2])
        except queue.Empty:
            pass
        if last:
            if bar["value"] != last[1]:
                bar["value"] = last[1]
            if v_status.get() != last[2]:
                v_status.set(last[2])
        if state["job"]:
            elapsed = int(time.time() - state["t0"])
            if state.get("last_elapsed") != elapsed:
                v_time.set(f"ใช้เวลาไปแล้ว {elapsed} วินาที")
                state["last_elapsed"] = elapsed
        root.after(120 if state["job"] else 500, poll)

    def finish(kind, msg):
        set_running(False)
        el = int(time.time() - state["t0"])
        v_time.set(f"ใช้เวลาทั้งหมด {el // 60} นาที {el % 60} วินาที")
        state["job"] = None
        if kind == "ok":
            bar["value"] = 100
            v_status.set("เสร็จสมบูรณ์")
            log(f"บันทึกแล้ว: {msg}")
            b_folder.config(state="normal")
            b_play.config(state="normal")
            messagebox.showinfo("เสร็จแล้ว", f"บันทึกไฟล์เรียบร้อย\n{msg}")
        elif kind == "cancelled":
            bar["value"] = 0
            v_status.set("ยกเลิกแล้ว")
        else:
            v_status.set("เกิดข้อผิดพลาด")
            log(msg)
            messagebox.showerror("เกิดข้อผิดพลาด", msg)

    b_start.config(command=start)
    b_cancel.config(command=cancel)
    b_check.config(command=run_check)
    b_folder.config(command=lambda: open_path(os.path.dirname(os.path.abspath(state["out"]))))
    b_play.config(command=lambda: open_path(state["out"]))

    def toggle_theme():
        state["theme"] = "dark" if state["theme"] == "light" else "light"
        apply_ui_theme(root, canvas, content, state["theme"], logbox, sliders,
                       theme_button, check_canvas, v_rec_autogain, b_folder,
                       scrollbar)
        store_settings()

    theme_button.config(command=toggle_theme)

    def on_close():
        if state["job"] and not messagebox.askyesno("ปิดโปรแกรม", "กำลังประมวลผลอยู่ ต้องการยกเลิกและปิดหรือไม่?"):
            return
        store_settings()
        if state["job"]:
            state["job"].cancel()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def refresh_titlebar_on_map(event):
        if event.widget is root:
            root.after_idle(lambda: apply_windows_chrome(
                root, state["theme"], THEMES[state["theme"]]))

    root.bind("<Map>", refresh_titlebar_on_map, add="+")
    refresh_presets()
    on_engine_change()
    apply_ui_theme(root, canvas, content, state["theme"], logbox, sliders,
                   theme_button, check_canvas, v_rec_autogain, b_folder,
                   scrollbar)

    # Finish initial geometry and theme while hidden so the first visible frame
    # does not expose the platform's default black/white erase background.
    root.update_idletasks()
    if scroll_cache["width_job"] is not None:
        root.after_cancel(scroll_cache["width_job"])
        scroll_cache["width_job"] = None
    initial_width = canvas.winfo_width()
    if initial_width > 1:
        canvas.itemconfigure(content_id, width=initial_width)
        scroll_cache["width"] = initial_width
        scroll_cache["pending_width"] = initial_width
        update_scroll_region()
        root.update_idletasks()
    root.deiconify()

    def warm():                                  # ตรวจ ffmpeg เบื้องหลังตอนเปิดโปรแกรม
        C.ffmpeg_caps()
        events.put(("caps",))
    threading.Thread(target=warm, daemon=True).start()

    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
