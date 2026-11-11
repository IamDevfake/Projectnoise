"""ชุดทดสอบอัตโนมัติ: python selftest.py
ใช้หลังอัปเดต Python / ffmpeg / numpy / scipy หรือ Windows เพื่อเช็กว่ายังทำงานครบ
ออกด้วยรหัส 0 เมื่อผ่านทั้งหมด (ข้อที่ข้ามเพราะเครื่องไม่มีเครื่องมือไม่นับเป็นล้มเหลว)"""
import array
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import denoise_common as C  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def skip(name, why):
    results.append((name, "SKIP", why))
    print(f"[SKIP] {name}  — {why}")


# ------------------------------------------------------------ ข้อมูลทดสอบ
SR, SECS, NOISE_SECS = 16000, 8, 3


def make_wav(path):
    """0-3 วินาที = noise ล้วน, 3-8 วินาที = โทนเสียง + noise"""
    rnd = random.Random(1)
    data = array.array("h")
    for i in range(SR * SECS):
        n = rnd.gauss(0, 0.03)
        t = i / SR
        tone = 0.3 * math.sin(2 * math.pi * 440 * t) if t >= NOISE_SECS else 0.0
        data.append(int(max(-1, min(1, n + tone)) * 32767))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def ff(*args):
    return subprocess.run([C.find_tool("ffmpeg"), "-y", "-v", "error", "-nostdin", *args],
                          capture_output=True, text=True)


def ffprobe_json(path):
    r = subprocess.run([C.find_tool("ffprobe"), "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path], capture_output=True, text=True)
    return json.loads(r.stdout)


def rms_first_seconds(path, secs):
    r = subprocess.run([C.find_tool("ffmpeg"), "-v", "error", "-nostdin", "-t", str(secs),
                        "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
                       capture_output=True)
    a = array.array("h")
    a.frombytes(r.stdout[:len(r.stdout) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / max(1, len(a))) / 32768


def run_cli(src, dst, engine, extra=()):
    cmd = [sys.executable, os.path.join(HERE, "denoise_video.py"), src, dst,
           "--engine", engine, "--progress-json", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


# ------------------------------------------------------------------ ชุดทดสอบ
def test_registry():
    import denoise_video
    check("ทะเบียนเอนจิน: ทุกเอนจินมีตัวรัน", set(denoise_video.RUNNERS) == set(C.ENGINES),
          f"{sorted(denoise_video.RUNNERS)} vs {sorted(C.ENGINES)}")
    bad = [(e, o) for e, s in C.ENGINES.items() for o in s["options"] if o not in C.OPTIONS]
    check("ทะเบียนเอนจิน: ตัวเลือกทุกตัวมีนิยาม", not bad, str(bad))


def test_progress_protocol():
    line = C.progress_line(2, 2, 41.55, 73.2)
    d = C.parse_progress_line("noise " + line)
    ok = d and d["stage"] == 2 and abs(d["pct"] - 41.5) < 0.11 and d["eta"] == 73
    check("โปรโตคอลความคืบหน้า JSON", bool(ok), str(d))
    check("โปรโตคอลความคืบหน้า: ข้อความปกติไม่ถูกตีความ", C.parse_progress_line("hello") is None
          and C.parse_progress_line(C.PROGRESS_TAG + "{bad") is None)
    check("เปอร์เซ็นต์รวม 2 ขั้น", abs(C.overall_percent(
        {"stage": 2, "stages": 2, "pct": 100.0}) - 100) < 1e-6)


def test_settings_and_presets():
    with tempfile.TemporaryDirectory() as td:
        os.environ["DENOISE_CONFIG_DIR"] = td
        s = C.load_settings()
        check("ตั้งค่า: ไม่มีไฟล์ -> ค่าเริ่มต้น", s == C.default_settings())
        s["values"]["reduction"] = 24
        s["engine"] = "ffmpeg"
        check("ตั้งค่า: บันทึกได้", C.save_settings(s))
        s2 = C.load_settings()
        check("ตั้งค่า: อ่านกลับตรงกัน", s2["engine"] == "ffmpeg" and s2["values"]["reduction"] == 24)

        with open(os.path.join(td, "settings.json"), "w", encoding="utf-8") as fh:
            fh.write('{"settings_version": 1, "reduction": 30, "alpha": 99, "engine": "nope"}')
        m = C.load_settings()
        check("ตั้งค่า: ย้ายจากรูปแบบเก่า v1 และบีบค่าให้อยู่ในช่วง",
              m["values"]["reduction"] == 30 and m["values"]["alpha"] == C.OPTIONS["alpha"]["max"]
              and m["engine"] == C.DEFAULT_ENGINE, str(m["values"]))

        with open(os.path.join(td, "settings.json"), "w", encoding="utf-8") as fh:
            fh.write("{ไฟล์เสีย")
        check("ตั้งค่า: ไฟล์เสีย -> ค่าเริ่มต้น + เก็บ .bak",
              C.load_settings() == C.default_settings()
              and os.path.exists(os.path.join(td, "settings.json.bak")))

        b, u = C.load_presets()
        check("พรีเซ็ต: มีของในตัว ไม่มีของผู้ใช้", len(b) == 5 and not u)
        u["ของฉัน"] = {"reduction": 999, "alpha": 1.5, "อื่น": 1}
        u[next(iter(b))] = {"reduction": 5}            # ชื่อชนกับของในตัว ต้องถูกทิ้ง
        C.save_user_presets(u)
        b2, u2 = C.load_presets()
        check("พรีเซ็ต: บันทึก/โหลด/บีบค่า/ทิ้งชื่อซ้ำ",
              list(u2) == ["ของฉัน"] and u2["ของฉัน"] == {"reduction": 40.0, "alpha": 1.5},
              str(u2))
        os.environ.pop("DENOISE_CONFIG_DIR")


def test_ffmpeg_capability_probe():
    if not C.ffmpeg_caps()["ffmpeg"]:
        return skip("ตรวจ filter/encoder ของ ffmpeg", "ไม่พบ ffmpeg")
    check("ตรวจ ffmpeg: มี afftdn/highpass/aac",
          C.has_filter("afftdn") and C.has_filter("highpass") and C.has_encoder("aac"))
    check("ตรวจ ffmpeg: ชื่อที่ไม่มีจริงต้องตอบว่าไม่มี",
          not C.has_filter("zz_no_such_filter") and not C.has_encoder("zz_no_such_encoder"))
    # จำลองกรณีอ่านตารางรายการไม่ออก/ไม่ครบ (เช่นรูปแบบเปลี่ยนใน ffmpeg รุ่นใหม่)
    real = C.ffmpeg_caps()
    fake = dict(real, filters=frozenset({"nothing"}), encoders=frozenset({"nothing"}))
    orig = C.ffmpeg_caps
    C.clear_caches()
    C.ffmpeg_caps = lambda: fake
    try:
        ok = C.has_filter("afftdn") and C.has_encoder("aac") and not C.has_filter("zz_no_such_filter")
    finally:
        C.ffmpeg_caps = orig
        C.clear_caches()
    check("ตรวจ ffmpeg: ตารางอ่านไม่ครบก็ยังตรวจตรง ๆ ได้ (ไม่ฟ้องผิด)", ok)


def test_uniform_fallback():
    try:
        import numpy as np
        import denoise_spectral as S
        from scipy.ndimage import uniform_filter1d as sp
    except ImportError as e:
        return skip("ตัวกรองสำรองเทียบ scipy", str(e))
    rng = np.random.default_rng(0)
    x = rng.random((2, 40, 33))
    ok = all(np.allclose(S.np_uniform_filter1d(x, sz, ax), sp(x, size=sz, axis=ax))
             for sz, ax in ((5, 2), (3, 1), (3, 2)))
    check("ตัวกรองสำรอง (ไม่ใช้ scipy) ให้ผลตรงกับ scipy", ok)


def test_engines():
    caps = C.ffmpeg_caps()
    if not (caps["ffmpeg"] and caps["ffprobe"]):
        return skip("รันเอนจิน", "ไม่พบ ffmpeg/ffprobe")
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "in.wav")
        make_wav(wav)
        mp4 = os.path.join(td, "in.mp4")
        r = ff("-f", "lavfi", "-i", f"color=c=blue:s=64x64:d={SECS}:r=10", "-i", wav,
               "-c:v", "mpeg4", "-c:a", "aac", "-shortest", mp4)
        has_video_clip = r.returncode == 0 and os.path.exists(mp4)
        base = rms_first_seconds(wav, NOISE_SECS - 0.5)
        for eid in C.ENGINES:
            problems = C.engine_problems(eid)
            if problems:
                skip(f"เอนจิน {eid}", "; ".join(problems))
                continue
            cases = [("เสียงอย่างเดียว", wav, os.path.join(td, f"{eid}.m4a"))]
            if has_video_clip:
                cases.append(("วิดีโอ", mp4, os.path.join(td, f"{eid}.mp4")))
            for label, src, dst in cases:
                r = run_cli(src, dst, eid, ("--reduction", "20"))
                name = f"เอนจิน {eid} / {label}"
                if r.returncode != 0 or not os.path.exists(dst):
                    check(name, False, (r.stderr or "")[-300:])
                    continue
                info = ffprobe_json(dst)
                kinds = {s["codec_type"] for s in info["streams"]}
                dur = float(info["format"]["duration"])
                want = {"audio"} | ({"video"} if label == "วิดีโอ" else set())
                prog = [C.parse_progress_line(l) for l in r.stderr.splitlines()]
                prog = [p for p in prog if p]
                good = (want <= kinds and abs(dur - SECS) < 0.7 and prog
                        and prog[-1]["pct"] >= 99.9)
                check(name, bool(good),
                      f"ยาว {dur:.2f}s, สตรีม {sorted(kinds)}, JSON progress {len(prog)} รายการ")
            if eid == "spectral":
                out = os.path.join(td, "spectral.m4a")
                after = rms_first_seconds(out, NOISE_SECS - 0.5) if os.path.exists(out) else 1
                db = 20 * math.log10(max(after, 1e-9) / base)
                check("spectral: noise ในช่วงเงียบลดลง ≥ 6 dB", db <= -6, f"{db:.1f} dB")
        # ความผิดพลาดต้องรายงานชัดเจน ไม่ค้าง
        r = run_cli(os.path.join(td, "ไม่มีไฟล์.wav"), os.path.join(td, "x.m4a"), "ffmpeg")
        check("ไฟล์ต้นทางหาย -> ออกด้วยข้อผิดพลาด", r.returncode != 0 and "ไม่พบไฟล์" in r.stderr)


def main():
    print(C.format_report(C.system_report()))
    print("-" * 60)
    for t in (test_registry, test_progress_protocol, test_settings_and_presets,
              test_ffmpeg_capability_probe, test_uniform_fallback, test_engines):
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, f"เกิดข้อยกเว้น: {e!r}")
    fails = [r for r in results if r[1] == "FAIL"]
    print("-" * 60)
    print(f"ผ่าน {sum(r[1] == 'PASS' for r in results)}  ข้าม {sum(r[1] == 'SKIP' for r in results)}"
          f"  ล้มเหลว {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())