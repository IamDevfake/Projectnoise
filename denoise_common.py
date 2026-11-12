"""ส่วนกลางที่ไม่พึ่ง numpy: รายการเอนจิน, ตัวเลือก, พรีเซ็ต, ไฟล์ตั้งค่า,
ตรวจระบบ (ffmpeg/ไลบรารี) และโปรโตคอลรายงานความคืบหน้า

เพิ่มเอนจินใหม่ 2 ขั้น:
  1) เพิ่มรายการใน ENGINES ด้านล่าง (ชื่อ, ไลบรารีที่ต้องมี, filter ของ ffmpeg, ตัวเลือกที่รองรับ)
  2) เพิ่มฟังก์ชันรันใน RUNNERS ของ denoise_video.py
หน้าจอจะสร้างปุ่มเลือกเอนจินและซ่อน/แสดงตัวเลื่อนตามรายการนี้เอง
"""
import functools
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys

APP_NAME = "DenoiseVideo"
VERSION = "1.1.0"
SETTINGS_VERSION = 5
MIN_PYTHON = (3, 8)
PROGRESS_TAG = "@@PROGRESS "
HERE = os.path.dirname(os.path.abspath(__file__))

AUDIO_ONLY_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}
BITRATES = ["128k", "192k", "256k", "320k"]

# ------------------------------------------------------------------ ตัวเลือก
OPTIONS = {
    "reduction": {"label": "ลด noise สูงสุด (dB)", "flag": "--reduction",
                  "min": 3, "max": 40, "step": 1, "default": 18,
                  "hint": "มาก = เงียบขึ้น"},
    "alpha": {"label": "ความแรง (alpha)", "flag": "--alpha",
              "min": 1.0, "max": 4.0, "step": 0.1, "default": 2.0,
              "hint": "มาก = ตัดแรงแต่เสียงอาจแหว่ง"},
    "release": {"label": "ความนุ่ม (release)", "flag": "--release",
                "min": 0.5, "max": 0.98, "step": 0.01, "default": 0.85,
                "hint": "มาก = เปลี่ยนนุ่มขึ้น"},
    "highpass": {"label": "ตัดเสียงต่ำกว่า (Hz)", "flag": "--highpass",
                 "min": 0, "max": 300, "step": 10, "default": 0,
                 "hint": "0 = ไม่ตัด, 80 = ตัดหึ่ง/ลม"},
    "volume": {"label": "เพิ่ม/ลดความดัง (dB)", "flag": "--volume",
               "min": -20, "max": 20, "step": 0.5, "default": 0,
               "hint": "0 = เท่าเดิม, +6 = ดังขึ้น 2 เท่า, -6 = เบาลง"},
    "gate": {"label": "Soft Gate (ตัวขยายเสียงเบา)", "flag": "--gate",
             "min": 0.0, "max": 3.0, "step": 0.1, "default": 1.0,
             "hint": "1 = ปิด (แนะนำถ้าเสียงกระตุก), มากกว่า 1 = ลดเสียงช่วงเงียบเพิ่ม"},
}

# ------------------------------------------------------------------ เอนจิน
ENGINES = {
    "spectral": {
        "label": "Spectral (คุณภาพดี ปรับได้)",
        "modules": ["numpy"],          # ไลบรารี Python ที่ต้องมี
        "filters": [],                 # filter ของ ffmpeg ที่ต้องมี
        "options": ["reduction", "alpha", "release", "highpass", "volume", "gate"],
        "noise_profile": True,         # รองรับการระบุช่วง noise เอง
    },
    "ffmpeg": {
        "label": "FFmpeg (เร็วสุด)",
        "modules": [],
        "filters": ["afftdn"],
        "options": ["reduction", "highpass", "volume", "gate"],
        "noise_profile": False,
    },
}
DEFAULT_ENGINE = "spectral"

BUILTIN_PRESETS = {
    "ทั่วไป (พัดลม/แอร์/ฟู่)": {"reduction": 20, "alpha": 2.2, "release": 0.85, "highpass": 70, "volume": 0, "gate": 1.0},
    "มีเสียงหึ่ง/ลมความถี่ต่ำ": {"reduction": 20, "alpha": 2.2, "release": 0.85, "highpass": 100, "volume": 0, "gate": 1.0},
    "ลด noise แรง": {"reduction": 26, "alpha": 3.0, "release": 0.85, "highpass": 80, "volume": 0, "gate": 1.0},
    "รักษาเสียงพูด (นุ่มนวล)": {"reduction": 14, "alpha": 1.6, "release": 0.90, "highpass": 50, "volume": 0, "gate": 0.8},
    "ลด noise + เพิ่มเสียงให้ดังขึ้น": {"reduction": 22, "alpha": 2.5, "release": 0.85, "highpass": 70, "volume": 6, "gate": 1.0},
}


def _num(v, default, lo=None, hi=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def clean_values(d):
    """คัดเฉพาะตัวเลือกที่รู้จัก และบีบค่าให้อยู่ในช่วงที่ยอมรับ"""
    out = {}
    if isinstance(d, dict):
        for k, spec in OPTIONS.items():
            if k in d:
                out[k] = _num(d[k], spec["default"], spec["min"], spec["max"])
    return out


def default_values():
    return {k: float(s["default"]) for k, s in OPTIONS.items()}


# ------------------------------------------------------------ ตำแหน่งไฟล์ตั้งค่า
def config_dir():
    env = os.environ.get("DENOISE_CONFIG_DIR")
    if env:
        return env
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/" + APP_NAME)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        APP_NAME)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        try:                                   # ไฟล์เสีย: เก็บสำรองไว้แล้วเริ่มใหม่
            os.replace(path, path + ".bak")
        except OSError:
            pass
        return None


def _write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ พรีเซ็ต
def _presets_path():
    return os.path.join(config_dir(), "presets.json")


def load_presets():
    """คืน (พรีเซ็ตในตัว, พรีเซ็ตของผู้ใช้)"""
    builtin = {n: clean_values(v) for n, v in BUILTIN_PRESETS.items()}
    user = {}
    raw = _read_json(_presets_path())
    if isinstance(raw, dict) and isinstance(raw.get("presets"), dict):
        for name, vals in raw["presets"].items():
            if isinstance(name, str) and name.strip() and len(name) <= 60 \
                    and name not in builtin:
                v = clean_values(vals)
                if v:
                    user[name] = v
    return builtin, user


def save_user_presets(user):
    return _write_json(_presets_path(),
                       {"settings_version": SETTINGS_VERSION, "presets": user})


# ------------------------------------------------------------------ ตั้งค่า
def default_settings():
    return {
        "settings_version": SETTINGS_VERSION,
        "theme": "light",
        "engine": DEFAULT_ENGINE,
        "preset": next(iter(BUILTIN_PRESETS)),
        "values": default_values(),
        "video_denoise": False,
        "preview": False,
        "auto_enhance": True,
        "wind_reduction": True,
        "auto_tune": True,
        "noise_manual": False,
        "noise_start": 0.0,
        "noise_dur": 2.0,
        "bitrate": "192k",
        "last_dir": "",
        "output_dir": "",
    }


# ฟังก์ชันแปลงโครงสร้างจากเวอร์ชัน n -> n+1 (เพิ่มเมื่อเปลี่ยนรูปแบบไฟล์ในอนาคต)
def _migrate_1_to_2(raw):
    # v1 เก็บค่าแบนไว้ที่ระดับบนสุด (reduction, alpha, ...) ย้ายเข้า "values"
    if "values" not in raw:
        raw["values"] = {k: raw[k] for k in OPTIONS if k in raw}
    return raw


_MIGRATIONS = {1: _migrate_1_to_2}


def migrate_settings(raw):
    """รับข้อมูลดิบจากไฟล์ (เวอร์ชันไหนก็ได้) คืนค่าตั้งค่าที่ตรวจแล้วและครบทุกคีย์"""
    s = default_settings()
    if not isinstance(raw, dict):
        return s
    raw = dict(raw)
    ver = raw.get("settings_version", 1)
    if not isinstance(ver, int) or ver < 1:
        ver = 1
    while ver < SETTINGS_VERSION:
        raw = _MIGRATIONS.get(ver, lambda r: r)(raw)
        ver += 1
    if raw.get("engine") in ENGINES:
        s["engine"] = raw["engine"]
    if raw.get("theme") in ("light", "dark"):
        s["theme"] = raw["theme"]
    if isinstance(raw.get("preset"), str):
        s["preset"] = raw["preset"]
    s["values"].update(clean_values(raw.get("values")))
    for k in ("video_denoise", "preview", "noise_manual", "auto_enhance",
              "wind_reduction", "auto_tune"):
        if isinstance(raw.get(k), bool):
            s[k] = raw[k]
    s["noise_start"] = _num(raw.get("noise_start"), 0.0, 0, 1e6)
    s["noise_dur"] = _num(raw.get("noise_dur"), 2.0, 0.2, 600)
    if raw.get("bitrate") in BITRATES:
        s["bitrate"] = raw["bitrate"]
    ld = raw.get("last_dir")
    if isinstance(ld, str) and os.path.isdir(ld):
        s["last_dir"] = ld
    od = raw.get("output_dir")
    if isinstance(od, str) and os.path.isdir(od):
        s["output_dir"] = od
    return s


def _settings_path():
    return os.path.join(config_dir(), "settings.json")


def load_settings():
    return migrate_settings(_read_json(_settings_path()))


def save_settings(s):
    return _write_json(_settings_path(), s)


# ------------------------------------------------------------------ เครื่องมือ
def hide_console():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def clear_caches():
    """ล้างผลตรวจทั้งหมด (เรียกเมื่อผู้ใช้กด 'ตรวจระบบ' เผื่อเพิ่งติดตั้ง/อัปเดต)"""
    ffmpeg_caps.cache_clear()
    has_filter.cache_clear()
    has_encoder.cache_clear()
    module_status.cache_clear()


def find_tool(name):
    """หา ffmpeg/ffprobe: ตัวแปร DENOISE_FFMPEG > โฟลเดอร์โปรแกรม > PATH"""
    exe = name + (".exe" if os.name == "nt" else "")
    env = os.environ.get("DENOISE_" + name.upper())
    if env and os.path.isfile(env):
        return env
    for d in (HERE, os.path.join(HERE, "ffmpeg"), os.path.join(HERE, "ffmpeg", "bin"),
              os.path.join(HERE, "bin")):
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def _run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", **hide_console())
    except Exception:  # noqa: BLE001
        return None


def _parse_table(text, flags_re):
    names = set()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(flags_re, parts[0]):
            names.add(parts[1])
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def ffmpeg_caps():
    """ตรวจความสามารถของ ffmpeg ที่ติดตั้ง (เก็บแคช; ล้างด้วย ffmpeg_caps.cache_clear())"""
    caps = {"ffmpeg": find_tool("ffmpeg"), "ffprobe": find_tool("ffprobe"),
            "version": "", "filters": frozenset(), "encoders": frozenset()}
    exe = caps["ffmpeg"]
    if exe:
        r = _run([exe, "-hide_banner", "-version"])
        if r and r.stdout:
            m = re.search(r"version\s+(\S+)", r.stdout.splitlines()[0])
            caps["version"] = m.group(1) if m else "ไม่ทราบ"
        r = _run([exe, "-hide_banner", "-filters"])
        caps["filters"] = _parse_table(r.stdout if r else "", r"[T.][S.][C.]")
        r = _run([exe, "-hide_banner", "-encoders"])
        caps["encoders"] = _parse_table(r.stdout if r else "", r"[VAS][.F][.S][.X][.B][.D]")
    return caps


def _ffmpeg_knows(kind, name):
    """ถาม ffmpeg ตรง ๆ ว่ามี filter/encoder ชื่อนี้ไหม (ไม่ขึ้นกับรูปแบบตารางของแต่ละรุ่น)"""
    exe = ffmpeg_caps()["ffmpeg"]
    if not exe:
        return False
    r = _run([exe, "-hide_banner", "-h", f"{kind}={name}"])
    if r is None:
        return False
    out = (r.stdout or "") + (r.stderr or "")
    return re.search(rf"^\s*{kind.capitalize()}\s+{re.escape(name)}\b", out, re.M) is not None


@functools.lru_cache(maxsize=None)
def has_filter(name):
    caps = ffmpeg_caps()
    return name in caps["filters"] or _ffmpeg_knows("filter", name)


@functools.lru_cache(maxsize=None)
def has_encoder(name):
    caps = ffmpeg_caps()
    return name in caps["encoders"] or _ffmpeg_knows("encoder", name)


@functools.lru_cache(maxsize=None)
def module_status(name):
    """คืน (เวอร์ชัน, ปัญหา) ลองโหลดจริง เพื่อจับกรณีติดตั้งอยู่แต่ใช้ไม่ได้
    (เช่น ไลบรารีไม่เข้ากับ Python รุ่นใหม่หลังอัปเดต)"""
    try:
        if importlib.util.find_spec(name) is None:
            return None, "ไม่พบ"
        importlib.import_module(name)
    except Exception as e:  # noqa: BLE001
        return None, f"ติดตั้งอยู่แต่โหลดไม่ได้ ({type(e).__name__}: {e})"
    try:
        from importlib import metadata
        return metadata.version(name.split(".")[0]), None
    except Exception:  # noqa: BLE001
        return "ไม่ทราบเวอร์ชัน", None


def engine_problems(engine, values=None, video_denoise=False):
    """รายการปัญหาที่ทำให้รันเอนจินนี้ไม่ได้ (ว่าง = พร้อม)"""
    problems = []
    if sys.version_info < MIN_PYTHON:
        problems.append("ต้องใช้ Python %d.%d ขึ้นไป" % MIN_PYTHON)
    spec = ENGINES.get(engine)
    if spec is None:
        return problems + [f"ไม่รู้จักเอนจิน {engine}"]
    caps = ffmpeg_caps()
    if not caps["ffmpeg"]:
        problems.append("ไม่พบ ffmpeg (ติดตั้งแล้วเปิดโปรแกรมใหม่ หรือวางไว้ข้างโปรแกรมนี้)")
    if not caps["ffprobe"]:
        problems.append("ไม่พบ ffprobe (มากับ ffmpeg)")
    for mod in spec["modules"]:
        _, err = module_status(mod)
        if err:
            problems.append(f"ไลบรารี {mod}: {err} (แก้ด้วย: pip install --upgrade {mod})")
    if caps["ffmpeg"]:
        need_f = list(spec["filters"])
        if (values or {}).get("highpass", 0) and float(values["highpass"]) > 0:
            need_f.append("highpass")
        if video_denoise:
            need_f.append("hqdn3d")
        for f in need_f:
            if not has_filter(f):
                problems.append(f"ffmpeg รุ่นนี้ไม่มี filter {f} (อัปเดต ffmpeg)")
        for e in ["aac"] + (["libx264"] if video_denoise else []):
            if not has_encoder(e):
                problems.append(f"ffmpeg รุ่นนี้ไม่มีตัวเข้ารหัส {e}")
    return problems


def system_report():
    """คืนรายการ (สถานะ, หัวข้อ, ข้อความ) สถานะ: ok / warn / bad"""
    rows = [("ok", "โปรแกรม", f"{APP_NAME} {VERSION}")]
    pv = platform.python_version()
    rows.append(("ok" if sys.version_info >= MIN_PYTHON else "bad", "Python", pv))
    rows.append(("ok", "ระบบ", platform.platform()))
    caps = ffmpeg_caps()
    if caps["ffmpeg"]:
        rows.append(("ok", "ffmpeg", f"{caps['version']}  ({caps['ffmpeg']})"))
    else:
        rows.append(("bad", "ffmpeg", "ไม่พบ"))
    rows.append(("ok" if caps["ffprobe"] else "bad", "ffprobe", caps["ffprobe"] or "ไม่พบ"))
    nv, nerr = module_status("numpy")
    rows.append(("ok" if nv else "bad", "numpy", nv or f"{nerr} (pip install --upgrade numpy)"))
    sv, serr = module_status("scipy.ndimage")
    rows.append(("ok" if sv else "warn", "scipy",
                 sv or f"{serr} — ไม่บังคับ โปรแกรมใช้ตัวสำรองแทนได้"))
    for eid, spec in ENGINES.items():
        p = engine_problems(eid)
        rows.append(("ok" if not p else "bad", f"เอนจิน {eid}",
                     "พร้อมใช้งาน" if not p else "; ".join(p)))
    p = engine_problems("ffmpeg", None, True)
    rows.append(("ok" if not any("hqdn3d" in x or "libx264" in x for x in p) else "warn",
                 "ลด noise ภาพ",
                 "พร้อมใช้งาน" if not any("hqdn3d" in x or "libx264" in x for x in p)
                 else "ffmpeg ไม่มี hqdn3d/libx264"))
    rows.append(("ok", "โฟลเดอร์ตั้งค่า", config_dir()))
    return rows


def format_report(rows):
    mark = {"ok": "[ OK ]", "warn": "[ !  ]", "bad": "[FAIL]"}
    w = max(len(r[1]) for r in rows)
    return "\n".join(f"{mark[s]} {label.ljust(w)}  {text}" for s, label, text in rows)


# ------------------------------------------------------- ffmpeg args ที่ใช้ร่วมกัน
def container_flags(dst):
    ext = os.path.splitext(dst)[1].lower()
    return ["-movflags", "+faststart"] if ext in (".mp4", ".mov", ".m4v") else []


def audio_args(dst, bitrate="192k"):
    ext = os.path.splitext(dst)[1].lower()
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", bitrate]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext == ".ogg":
        return ["-c:a", "libvorbis", "-b:a", bitrate]
    return ["-c:a", "aac", "-b:a", bitrate]


def video_args(has_video, video_denoise):
    if not has_video:
        return []
    if video_denoise:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-vf", "hqdn3d=4:3:6:4.5"]
    return ["-c:v", "copy"]


# ---------------------------------------------- โปรโตคอลความคืบหน้า (เอนจิน -> หน้าจอ)
def progress_line(stage, stages, pct, eta=None):
    d = {"v": 1, "stage": stage, "stages": stages, "pct": round(pct, 1)}
    if eta is not None:
        d["eta"] = round(eta)
    return PROGRESS_TAG + json.dumps(d, separators=(",", ":"))


def parse_progress_line(line):
    i = line.find(PROGRESS_TAG)
    if i < 0:
        return None
    try:
        d = json.loads(line[i + len(PROGRESS_TAG):])
        return {"stage": int(d["stage"]), "stages": int(d["stages"]),
                "pct": float(d["pct"]), "eta": d.get("eta")}
    except (ValueError, KeyError, TypeError):
        return None


def overall_percent(d):
    if d["stages"] == 2:
        return d["pct"] * 0.15 if d["stage"] == 1 else 15 + d["pct"] * 0.85
    return d["pct"]


def progress_text(d):
    p = d["pct"]
    if d["stages"] == 2 and d["stage"] == 1:
        return f"ขั้นที่ 1/2 วิเคราะห์ noise profile {p:.0f}%"
    if d["stages"] == 2:
        eta = f"  เหลือประมาณ {d['eta']}s" if d.get("eta") is not None else ""
        return f"ขั้นที่ 2/2 ลด noise {p:.0f}%{eta}"
    return f"กำลังประมวลผล {p:.0f}%"


class Reporter:
    """รายงานความคืบหน้าทาง stderr: โหมดคน (\\r) หรือโหมด JSON สำหรับหน้าจอ"""

    def __init__(self, json_mode=False):
        self.json_mode = json_mode

    def update(self, stage, stages, pct, eta=None):
        pct = max(0.0, min(100.0, pct))
        if self.json_mode:
            print(progress_line(stage, stages, pct, eta), file=sys.stderr, flush=True)
            return
        if stages == 1:
            text = f"\r  กำลังประมวลผล {pct:5.1f}%"
        elif stage == 1:
            text = f"\r  [1/2] วิเคราะห์ noise profile {pct:5.1f}%"
        else:
            text = f"\r  [2/2] ลด noise {pct:5.1f}%  เหลือประมาณ {(eta or 0):4.0f}s"
        print(text, end="", file=sys.stderr, flush=True)

    def finish(self, stage, stages, seconds):
        if self.json_mode:
            self.update(stage, stages, 100.0, 0)
            return
        name = ("กำลังประมวลผล" if stages == 1 else
                "[1/2] วิเคราะห์ noise profile" if stage == 1 else "[2/2] ลด noise")
        print(f"\r  {name} 100.0% ({seconds:.0f}s)            ", file=sys.stderr)
