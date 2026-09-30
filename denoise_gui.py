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
APP_TITLE = ""


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

PREVIEW_SECONDS = 300


# ============================================================ logic (ไม่ใช้ tkinter)
def default_output(src, preview=False):
    base, ext = os.path.splitext(src)
    if ext.lower() in C.AUDIO_ONLY_EXT:
        ext = ".m4a"
    return f"{base}_{'preview' if preview else 'denoised'}{ext or '.mp4'}"


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


def set_blank_icon(root, tk):
    """ซ่อนไอคอนขนนกของ Tk ด้วยไอคอนโปร่งใส"""
    try:
        if os.name == "nt":
            path = os.path.join(tempfile.gettempdir(), "denoise_blank.ico")
            hdr = struct.pack("<HHH", 0, 1, 1)
            entry = struct.pack("<BBBBHHII", 1, 1, 0, 0, 1, 32, 48, 22)
            bmp = struct.pack("<IiiHHIIiiII", 40, 1, 2, 1, 32, 0, 0, 0, 0, 0, 0)
            data = hdr + entry + bmp + b"\x00\x00\x00\x00" + b"\xff\x00\x00\x00"
            with open(path, "wb") as fh:
                fh.write(data)
            root.iconbitmap(default=path)
            root.after(0, lambda: _drop_title_icon_slot(root))
        else:
            root.iconphoto(True, tk.PhotoImage(width=1, height=1))
    except Exception:  # noqa: BLE001
        pass


def open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def main():
    if sys.version_info < C.MIN_PYTHON:
        _fatal("ต้องใช้ Python %d.%d ขึ้นไป" % C.MIN_PYTHON)
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    root = tk.Tk()
    root.title(APP_TITLE)
    set_blank_icon(root, tk)
    root.minsize(660, 760)
    pad = {"padx": 10, "pady": 4}

    events = queue.Queue()
    state = {"job": None, "t0": 0.0, "out": "", "user_presets": set()}
    cfg = C.load_settings()
    presets = {}

    v_in, v_out = tk.StringVar(), tk.StringVar()
    v_info = tk.StringVar(value="ยังไม่ได้เลือกไฟล์")
    v_preset = tk.StringVar(value=cfg["preset"])
    v_engine = tk.StringVar(value=cfg["engine"])
    v_vals = {k: tk.DoubleVar(value=cfg["values"][k]) for k in C.OPTIONS}
    v_manual = tk.BooleanVar(value=cfg["noise_manual"])
    v_ns, v_nd = tk.DoubleVar(value=cfg["noise_start"]), tk.DoubleVar(value=cfg["noise_dur"])
    v_vid, v_prev = tk.BooleanVar(value=cfg["video_denoise"]), tk.BooleanVar(value=cfg["preview"])
    v_br = tk.StringVar(value=cfg["bitrate"])
    v_status = tk.StringVar(value="พร้อมทำงาน")
    v_time = tk.StringVar(value="")

    # ---- 1. ไฟล์
    f = ttk.LabelFrame(root, text="1. เลือกไฟล์")
    f.pack(fill="x", **pad)
    f.columnconfigure(1, weight=1)
    ttk.Label(f, text="วิดีโอต้นฉบับ").grid(row=0, column=0, sticky="w", padx=6, pady=3)
    ttk.Entry(f, textvariable=v_in).grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Label(f, text="บันทึกเป็น").grid(row=1, column=0, sticky="w", padx=6, pady=3)
    ttk.Entry(f, textvariable=v_out).grid(row=1, column=1, sticky="ew", padx=6)
    ttk.Label(f, textvariable=v_info, foreground="#666").grid(
        row=2, column=1, sticky="w", padx=6, pady=(0, 4))

    def pick_in():
        p = filedialog.askopenfilename(
            title="เลือกวิดีโอ", initialdir=cfg["last_dir"] or None,
            filetypes=[("วิดีโอ/เสียง", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.flv "
                                       "*.mp3 *.wav *.m4a *.flac"), ("ทุกไฟล์", "*.*")])
        if p:
            cfg["last_dir"] = os.path.dirname(p)
            v_in.set(p)
            v_out.set(default_output(p, v_prev.get()))
            v_info.set("กำลังอ่านข้อมูลไฟล์...")
            threading.Thread(
                target=lambda path=p: events.put(("info", path, get_duration(path))),
                daemon=True).start()

    def pick_out():
        p = filedialog.asksaveasfilename(title="บันทึกเป็น", initialfile=os.path.basename(v_out.get()),
                                         initialdir=cfg["last_dir"] or None)
        if p:
            v_out.set(p)

    ttk.Button(f, text="เลือก...", command=pick_in).grid(row=0, column=2, padx=6)
    ttk.Button(f, text="เลือก...", command=pick_out).grid(row=1, column=2, padx=6)

    # ---- 2. ตั้งค่า (สร้างจากรายการใน denoise_common)
    s = ttk.LabelFrame(root, text="2. ตั้งค่า")
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
                      resolution=spec["step"], orient="horizontal")
        sc.grid(row=row, column=1, sticky="ew", padx=6)
        ht = ttk.Label(s, text=spec["hint"], foreground="#666")
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
    o2.grid(row=nrow + 1, column=0, columnspan=3, sticky="w", padx=6, pady=3)
    ttk.Checkbutton(o2, text="ลด noise ภาพด้วย (ช้าลงมาก)", variable=v_vid).pack(side="left")
    ttk.Label(o2, text="   บิตเรตเสียง").pack(side="left")
    ttk.Combobox(o2, textvariable=v_br, values=C.BITRATES,
                 width=6, state="readonly").pack(side="left", padx=4)
    ttk.Checkbutton(s, text=f"ทดสอบเฉพาะ {PREVIEW_SECONDS // 60} นาทีแรกก่อน (แนะนำ ใช้ปรับค่าให้เร็ว)",
                    variable=v_prev,
                    command=lambda: v_in.get() and v_out.set(
                        default_output(v_in.get(), v_prev.get()))
                    ).grid(row=nrow + 2, column=0, columnspan=3, sticky="w", padx=6, pady=3)

    def on_engine_change(*_):
        spec = C.ENGINES.get(v_engine.get(), {})
        for key, widgets in opt_rows.items():
            for w in widgets:
                (w.grid if key in spec.get("options", ()) else w.grid_remove)()
        (nr.grid if spec.get("noise_profile") else nr.grid_remove)()

    v_engine.trace_add("write", on_engine_change)

    # ---- 3. ประมวลผล
    r = ttk.LabelFrame(root, text="3. ประมวลผล")
    r.pack(fill="both", expand=True, **pad)
    bar = ttk.Progressbar(r, maximum=100)
    bar.pack(fill="x", padx=6, pady=(6, 2))
    ttk.Label(r, textvariable=v_status).pack(anchor="w", padx=6)
    ttk.Label(r, textvariable=v_time, foreground="#666").pack(anchor="w", padx=6)
    logbox = tk.Text(r, height=6, state="disabled", wrap="word")
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
    b_start = ttk.Button(bf, text="เริ่มลด noise")
    b_start.pack(side="left")
    b_cancel = ttk.Button(bf, text="ยกเลิก", state="disabled")
    b_cancel.pack(side="left", padx=6)
    b_check = ttk.Button(bf, text="ตรวจระบบ")
    b_check.pack(side="left")
    b_folder = ttk.Button(bf, text="เปิดโฟลเดอร์", state="disabled")
    b_folder.pack(side="right")
    b_play = ttk.Button(bf, text="เปิดไฟล์", state="disabled")
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

    def collect():
        return {"engine": v_engine.get(),
                "values": {k: v.get() for k, v in v_vals.items()},
                "bitrate": v_br.get(), "noise_manual": v_manual.get(),
                "noise_start": v_ns.get(), "noise_dur": v_nd.get(),
                "video_denoise": v_vid.get(), "preview": v_prev.get()}

    def store_settings():
        try:
            o = collect()
        except tk.TclError:
            return
        C.save_settings({
            "settings_version": C.SETTINGS_VERSION, "engine": o["engine"],
            "preset": v_preset.get(), "values": o["values"],
            "video_denoise": o["video_denoise"], "preview": o["preview"],
            "noise_manual": o["noise_manual"], "noise_start": o["noise_start"],
            "noise_dur": o["noise_dur"], "bitrate": o["bitrate"],
            "last_dir": cfg["last_dir"]})

    def start():
        src, dst = v_in.get().strip(), v_out.get().strip()
        if not os.path.isfile(src):
            return messagebox.showwarning("ยังไม่พร้อม", "กรุณาเลือกไฟล์วิดีโอต้นฉบับ")
        if not dst:
            return messagebox.showwarning("ยังไม่พร้อม", "กรุณาระบุชื่อไฟล์ที่จะบันทึก")
        if os.path.abspath(src) == os.path.abspath(dst):
            return messagebox.showwarning("ชื่อซ้ำ", "ไฟล์ปลายทางต้องไม่ใช่ไฟล์เดียวกับต้นฉบับ")
        try:
            opts = collect()
        except tk.TclError:
            return messagebox.showwarning("ค่าไม่ถูกต้อง", "ช่องวินาทีของช่วง noise ต้องเป็นตัวเลข")
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
        box = tk.Text(win, wrap="none", font=("Courier New", 10) if os.name == "nt" else "TkFixedFont")
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
            bar["value"] = last[1]
            v_status.set(last[2])
        if state["job"]:
            v_time.set(f"ใช้เวลาไปแล้ว {int(time.time() - state['t0'])} วินาที")
        root.after(150, poll)

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

    def on_close():
        if state["job"] and not messagebox.askyesno("ปิดโปรแกรม", "กำลังประมวลผลอยู่ ต้องการยกเลิกและปิดหรือไม่?"):
            return
        store_settings()
        if state["job"]:
            state["job"].cancel()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_presets()
    on_engine_change()

    def warm():                                  # ตรวจ ffmpeg เบื้องหลังตอนเปิดโปรแกรม
        C.ffmpeg_caps()
        events.put(("caps",))
    threading.Thread(target=warm, daemon=True).start()

    poll()
    root.mainloop()


if __name__ == "__main__":
    main()