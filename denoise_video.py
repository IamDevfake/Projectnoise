"""ลดเสียงรบกวนในวิดีโอ/เสียงยาว ๆ (สตรีม, หน่วยความจำคงที่) — โปรแกรมบรรทัดคำสั่ง

ตัวอย่าง:  python denoise_video.py in.mp4 out.mp4 --reduction 18 --highpass 80
ตรวจระบบ:   python denoise_video.py --check
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import denoise_common as C


# ----------------------------------------------------------------- ffmpeg utils
def probe(path):
    ffprobe = C.find_tool("ffprobe") or "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, check=True, encoding="utf-8",
            errors="replace", **C.hide_console()).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"อ่านข้อมูลไฟล์ไม่สำเร็จ: {getattr(e, 'stderr', '') or e}")
    info = json.loads(out)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not audio:
        sys.exit("ไม่พบสตรีมเสียงในไฟล์นี้")
    a = audio[0]
    dur = float(info["format"].get("duration") or a.get("duration") or 0)
    has_video = any(s["codec_type"] == "video" for s in info["streams"])
    return dur, int(a["sample_rate"]), min(int(a["channels"]), 2), has_video


# ------------------------------------------------------------------ engines
def _run_spectral(a, dur, sr, ch, has_video, tmp_err, rep):
    from denoise_spectral import run_spectral      # โหลด numpy เมื่อจำเป็นเท่านั้น
    return run_spectral(a, dur, sr, ch, has_video, tmp_err, rep)


def _run_ffmpeg(a, dur, sr, ch, has_video, tmp_err, rep):
    nr = min(97.0, max(0.01, a.reduction))
    af = []
    if a.highpass > 0:
        af.append(f"highpass=f={a.highpass}")
    af.append(f"afftdn=nr={nr}:nf=-45:tn=1")
    vol_db = getattr(a, "volume", 0.0)
    if vol_db != 0.0:
        af.append(f"volume={vol_db}dB")
    cmd = [C.find_tool("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-nostats",
           "-progress", "pipe:1", "-nostdin", "-i", a.input,
           "-map", "0:v:0?", "-map", "0:a:0", "-map_metadata", "0"]
    cmd += C.video_args(has_video, a.video_denoise)
    cmd += ["-af", ",".join(af)] + C.audio_args(a.output, a.audio_bitrate)
    cmd += ["-max_muxing_queue_size", "9999"]
    cmd += C.container_flags(a.output) + [a.output]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=tmp_err, text=True,
                            encoding="utf-8", errors="replace", **C.hide_console())
    t0, last = time.time(), -1.0
    for line in proc.stdout:
        k, _, v = line.strip().partition("=")
        if k in ("out_time_us", "out_time_ms") and dur:
            try:
                sec = int(v) / 1e6
            except ValueError:
                continue
            pct = min(100.0, sec / dur * 100)
            if pct - last >= 0.1:
                last = pct
                el = time.time() - t0
                rep.update(1, 1, pct, el / pct * (100 - pct) if pct > 0 else None)
    rc = proc.wait()
    rep.finish(1, 1, time.time() - t0)
    return rc


# ชื่อเอนจิน -> ฟังก์ชันรัน (ต้องตรงกับ ENGINES ใน denoise_common.py)
RUNNERS = {"spectral": _run_spectral, "ffmpeg": _run_ffmpeg}


# --------------------------------------------------------------------- main
def build_parser():
    ap = argparse.ArgumentParser(description="ลดเสียงรบกวนในวิดีโอยาว (สตรีม, หน่วยความจำคงที่)")
    ap.add_argument("input", nargs="?")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--engine", choices=list(C.ENGINES), default=C.DEFAULT_ENGINE,
                    help="spectral = คุณภาพดีกว่า/ปรับได้; ffmpeg = เร็วสุด (afftdn)")
    ap.add_argument("--reduction", type=float, default=18.0,
                    help="ลด noise สูงสุดกี่ dB (ค่าเริ่มต้น 18)")
    ap.add_argument("--alpha", type=float, default=2.0,
                    help="over-subtraction; มาก = ตัด noise แรงขึ้นแต่เสียงอาจแหว่ง (1.0-3.0)")
    ap.add_argument("--release", type=float, default=0.85,
                    help="ความนุ่มของการปิดเกท 0-0.99 (มาก = นุ่มขึ้น)")
    ap.add_argument("--highpass", type=float, default=0,
                    help="ตัดความถี่ต่ำกว่านี้ (Hz) เช่น 80 สำหรับเสียงหึ่ง/ลม")
    ap.add_argument("--volume", type=float, default=0.0,
                    help="เพิ่ม/ลดความดัง เป็น dB (0 = เท่าเดิม, +6 = ดังขึ้น 2 เท่า)")
    ap.add_argument("--noise-start", type=float, default=None,
                    help="วินาทีเริ่มต้นของช่วงที่เป็น noise ล้วน (ไม่ระบุ = ตรวจจับอัตโนมัติ)")
    ap.add_argument("--noise-dur", type=float, default=2.0,
                    help="ความยาวช่วง noise (วินาที) ใช้คู่กับ --noise-start")
    ap.add_argument("--video-denoise", action="store_true",
                    help="ลด noise ภาพด้วย hqdn3d (re-encode x264, ช้าลงมาก)")
    ap.add_argument("--audio-bitrate", default="192k")
    ap.add_argument("--progress-json", action="store_true",
                    help="รายงานความคืบหน้าเป็นบรรทัด JSON (สำหรับหน้าจอ)")
    ap.add_argument("--check", action="store_true", help="ตรวจระบบแล้วออก")
    ap.add_argument("--version", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)

    if a.version:
        print(f"{C.APP_NAME} {C.VERSION}")
        return 0
    if a.check:
        rows = C.system_report()
        print(C.format_report(rows))
        return 0 if all(s != "bad" for s, _, _ in rows) else 1
    if not a.input or not a.output:
        ap.error("ต้องระบุไฟล์ input และ output")

    if not os.path.isfile(a.input):
        sys.exit(f"ไม่พบไฟล์: {a.input}")
    if os.path.abspath(a.input) == os.path.abspath(a.output):
        sys.exit("ไฟล์ output ต้องไม่ใช่ไฟล์เดียวกับ input")
    problems = C.engine_problems(a.engine, {"highpass": a.highpass}, a.video_denoise)
    if problems:
        sys.exit("\n".join(problems))
    runner = RUNNERS.get(a.engine)
    if runner is None:
        sys.exit(f"เอนจิน {a.engine} ยังไม่มีตัวรัน")

    dur, sr, ch, has_video = probe(a.input)
    print(f"อินพุต: {dur / 60:.1f} นาที, {sr} Hz, {ch} ช่อง, "
          f"{'มีวิดีโอ' if has_video else 'เสียงอย่างเดียว'}, engine={a.engine}",
          file=sys.stderr)

    rep = C.Reporter(a.progress_json)
    t0 = time.time()
    with tempfile.TemporaryFile() as tmp_err:
        rc = runner(a, dur, sr, ch, has_video, tmp_err, rep)
        if rc != 0:
            tmp_err.seek(0)
            sys.stderr.write(tmp_err.read().decode(errors="replace"))
            sys.exit(f"ffmpeg ล้มเหลว (รหัส {rc})")
    print(f"เสร็จสิ้นใน {time.time() - t0:.0f} วินาที -> {a.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())