"""ชุดทดสอบและจำลองการใช้งานจริงแบบครบวงจร (Sandbox End-to-End Testing & Benchmark)
ทดสอบ:
1. โมดูลอัดเสียง (AudioRecorder): ไมค์, การตรวจวัดระดับเสียง (Peak/RMS), บันทึกไฟล์ WAV
2. การสร้างไฟล์เสียงจริงที่มีเสียงรบกวนภายนอก (เสียงพูด + เสียงพัดลม/แอร์ + เสียงฮัม 50Hz/60Hz + เสียงฟู่)
3. การประมวลผลตัดเสียงรบกวนผ่านอัลกอริทึม (Spectral Subtraction / Gating และ FFmpeg afftdn)
4. การส่งออกไฟล์ผลลัพธ์ไปยังโฟลเดอร์ปลายทางที่กำหนด (Custom Output Directory) ทั้ง .wav, .mp3, .m4a
5. การวัดผลประสิทธิภาพเชิงปริมาณ (Noise Reduction dB, SNR Improvement, Signal Preservation)
"""
import array
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import wave
import numpy as np

import denoise_common as C
from audio_recorder import AudioRecorder

SR = 16000
SECS = 6
NOISE_SECS = 2.0


def create_realistic_audio_with_noise(output_path):
    """สร้างสัญญาณเสียงจำลองที่มีทั้งเสียงพูดและเสียงรบกวนภายนอกจริง:
    - 0 ถึง 2 วินาที: เสียงรบกวนภายนอกล้วน (Ambient Noise: พัดลม + ฮัม + ฟู่)
    - 2 ถึง 6 วินาที: เสียงพูดจำลอง (Harmonics/Formants 200Hz, 700Hz, 2200Hz) ผสมกับเสียงรบกวนภายนอก
    """
    total_samples = int(SR * SECS)
    rnd = random.Random(42)

    # 1. สร้างเสียงรบกวนภายนอก (Ambient Noise)
    # 1.1 เสียงฟู่ (White Noise)
    noise_white = np.array([rnd.gauss(0, 0.02) for _ in range(total_samples)], dtype=np.float32)
    
    # 1.2 เสียงฮัมความถี่ต่ำจากแอร์หรือระบบไฟ (Low-frequency Hum 60Hz + 120Hz)
    t = np.arange(total_samples) / SR
    noise_hum = (0.04 * np.sin(2 * np.pi * 60 * t) + 0.02 * np.sin(2 * np.pi * 120 * t)).astype(np.float32)

    # 1.3 เสียงลมพัดลม (Pinkish Noise - lowpass filtered)
    noise_fan = np.convolve(noise_white, np.ones(15) / 15.0, mode="same") * 1.5

    ambient_noise = noise_white + noise_hum + noise_fan

    # 2. สร้างเสียงพูดจำลอง (Speech Formant Simulation: วลีพูดจังหวะคน)
    speech = np.zeros(total_samples, dtype=np.float32)
    speech_start_idx = int(NOISE_SECS * SR)

    for i in range(speech_start_idx, total_samples):
        cur_t = (i - speech_start_idx) / SR
        # จำลองการพูดเป็นคำๆ (Envelope Modulation)
        envelope = max(0.0, np.sin(2 * np.pi * 1.5 * cur_t)) ** 2
        # ฮาร์มอนิกของเสียงพูด
        f0 = 150 + 20 * np.sin(2 * np.pi * 2.0 * cur_t)  # ความถี่เสียงหลักแปรผัน
        vocal = (0.35 * np.sin(2 * np.pi * f0 * cur_t) +
                 0.25 * np.sin(2 * np.pi * (2 * f0) * cur_t) +
                 0.15 * np.sin(2 * np.pi * 750 * cur_t) +
                 0.10 * np.sin(2 * np.pi * 2400 * cur_t))
        speech[i] = vocal * envelope

    # รวมเสียงสัญญาณ
    mixed = np.clip(speech + ambient_noise, -1.0, 1.0)
    int16_data = np.int16(mixed * 32767)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(int16_data.tobytes())

    return mixed, ambient_noise, speech


def measure_rms_db(path, start_sec, dur_sec):
    """วัดระดับพลังงาน RMS เป็นเดซิเบล (dB) ในช่วงเวลาที่กำหนด"""
    cmd = [C.find_tool("ffmpeg") or "ffmpeg", "-v", "error", "-nostdin",
           "-ss", str(start_sec), "-t", str(dur_sec),
           "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    if not r.stdout:
        return -999.0
    a = array.array("h")
    a.frombytes(r.stdout[:len(r.stdout) // 2 * 2])
    if not a:
        return -999.0
    rms = math.sqrt(sum(x * x for x in a) / len(a)) / 32768.0
    return 20.0 * math.log10(max(rms, 1e-9))


def run_tests():
    print("=" * 70)
    print("🚀 เริ่มการทดสอบระบบตัดเสียงรบกวน & ระบบอัดเสียงใน Sandbox")
    print("=" * 70)

    sandbox_test_dir = os.path.join(tempfile.gettempdir(), "denoise_sandbox_test")
    custom_save_dir = os.path.join(sandbox_test_dir, "my_custom_records")
    os.makedirs(custom_save_dir, exist_ok=True)

    test_results = []

    # -----------------------------------------------------------------
    # Test 1: ทดสอบโมดูลอัดเสียง (AudioRecorder)
    # -----------------------------------------------------------------
    print("\n[TEST 1] ทดสอบโมดูลอัดเสียง (AudioRecorder)")
    rec = AudioRecorder(sample_rate=16000, channels=1)
    avail, amsg = rec.is_available()
    print(f"  - ตรวจสอบความพร้อมของฮาร์ดแวร์/ไดรเวอร์เสียง: {avail} ({amsg})")
    
    ok_start, smsg = rec.start_recording()
    print(f"  - ทดสอบเริ่มอัดเสียง: {ok_start} ({smsg})")
    time.sleep(0.6)
    peak, rms = rec.get_levels()
    dur = rec.get_duration()
    print(f"  - ตรวจจับระดับเสียงขณะอัด: Peak={peak:.4f}, RMS={rms:.4f}, ระยะเวลา={dur:.2f}s")
    ok_stop, stop_msg = rec.stop_recording()
    print(f"  - ทดสอบหยุดอัด: {ok_stop} ({stop_msg})")

    # ทดสอบบันทึกเสียงดิบลงโฟลเดอร์ที่ผู้ใช้เลือก
    rec_save_path = os.path.join(custom_save_dir, "raw_mic_recording.wav")
    ok_save, save_msg = rec.save_to_file(rec_save_path)
    file_exists = os.path.isfile(rec_save_path) and os.path.getsize(rec_save_path) > 1000
    print(f"  - บันทึกลงโฟลเดอร์ปลายทาง ({rec_save_path}): {ok_save} (ขนาด={os.path.getsize(rec_save_path) if file_exists else 0} bytes)")
    
    t1_pass = ok_start and ok_stop and file_exists
    test_results.append(("Audio Recording & Custom Folder Export", t1_pass))

    # -----------------------------------------------------------------
    # Test 2: สร้างไฟล์เสียงทดสอบที่มีเสียงรบกวนภายนอกจริง (Hum + Fan + Hiss)
    # -----------------------------------------------------------------
    print("\n[TEST 2] สร้างไฟล์เสียงทดสอบที่มีเสียงรบกวนภายนอกจริง")
    noisy_input_wav = os.path.join(sandbox_test_dir, "test_input_noisy.wav")
    create_realistic_audio_with_noise(noisy_input_wav)
    input_dur = os.path.getsize(noisy_input_wav) / (SR * 2)
    print(f"  - สร้างไฟล์เสียง: {noisy_input_wav}")
    print(f"  - ความยาว: {input_dur:.2f} วินาที, Sample rate: {SR} Hz")

    # วัดระดับเสียงก่อนตัด
    noise_db_before = measure_rms_db(noisy_input_wav, 0.2, 1.6)
    speech_db_before = measure_rms_db(noisy_input_wav, 2.5, 3.0)
    snr_before = speech_db_before - noise_db_before
    print(f"  - [ก่อนตัด] ระดับ Noise ในช่วงเงียบ: {noise_db_before:.2f} dB")
    print(f"  - [ก่อนตัด] ระดับเสียงพูดรวม Noise: {speech_db_before:.2f} dB")
    print(f"  - [ก่อนตัด] ประมาณการ SNR เดิม: {snr_before:.2f} dB")
    test_results.append(("Realistic Noise Benchmark Creation", os.path.isfile(noisy_input_wav)))

    # -----------------------------------------------------------------
    # Test 3: ทดสอบตัดเสียงรบกวนด้วย Spectral Denoising เอนจิน
    # -----------------------------------------------------------------
    print("\n[TEST 3] ทดสอบอัลกอริทึม Spectral Denoising -> บันทึกลงโฟลเดอร์ปลายทาง")
    spectral_output = os.path.join(custom_save_dir, "denoised_spectral.wav")
    if os.path.exists(spectral_output):
        os.remove(spectral_output)

    cmd_spectral = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "denoise_video.py"),
        noisy_input_wav,
        spectral_output,
        "--engine", "spectral",
        "--reduction", "22",
        "--alpha", "2.2",
        "--highpass", "70",
    ]
    t0 = time.time()
    res_spectral = subprocess.run(cmd_spectral, capture_output=True, text=True)
    el_spectral = time.time() - t0
    
    spectral_ok = res_spectral.returncode == 0 and os.path.isfile(spectral_output)
    print(f"  - สถานะการรัน: {'สำเร็จ (Code 0)' if res_spectral.returncode == 0 else f'ล้มเหลว ({res_spectral.returncode})'}")
    print(f"  - ใช้เวลาประมวลผล: {el_spectral:.2f} วินาที")
    
    if spectral_ok:
        noise_db_after = measure_rms_db(spectral_output, 0.2, 1.6)
        speech_db_after = measure_rms_db(spectral_output, 2.5, 3.0)
        noise_reduced = noise_db_before - noise_db_after
        snr_after = speech_db_after - noise_db_after
        snr_gain = snr_after - snr_before
        print(f"  - [หลังตัด] ระดับ Noise ในช่วงเงียบ: {noise_db_after:.2f} dB")
        print(f"  - [หลังตัด] ตัดเสียงรบกวนออกไปได้: {noise_reduced:.2f} dB (ลดลงอย่างชัดเจน)")
        print(f"  - [หลังตัด] ระดับเสียงพูด: {speech_db_after:.2f} dB")
        print(f"  - [หลังตัด] SNR หลังประมวลผล: {snr_after:.2f} dB (พัฒนาขึ้น +{snr_gain:.2f} dB)")
        t3_pass = noise_reduced >= 10.0 and speech_db_after > -30.0
    else:
        print(f"  - Error stderr: {res_spectral.stderr}")
        t3_pass = False
    test_results.append(("Spectral Denoising Performance (≥10 dB reduction)", t3_pass))

    # -----------------------------------------------------------------
    # Test 4: ทดสอบตัดเสียงรบกวนด้วย FFmpeg afftdn เอนจิน
    # -----------------------------------------------------------------
    print("\n[TEST 4] ทดสอบอัลกอริทึม FFmpeg afftdn -> บันทึกลงโฟลเดอร์ปลายทาง")
    ffmpeg_output = os.path.join(custom_save_dir, "denoised_ffmpeg.wav")
    if os.path.exists(ffmpeg_output):
        os.remove(ffmpeg_output)

    cmd_ffmpeg = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "denoise_video.py"),
        noisy_input_wav,
        ffmpeg_output,
        "--engine", "ffmpeg",
        "--reduction", "18",
        "--highpass", "70",
    ]
    t0 = time.time()
    res_ffmpeg = subprocess.run(cmd_ffmpeg, capture_output=True, text=True)
    el_ffmpeg = time.time() - t0
    
    ffmpeg_ok = res_ffmpeg.returncode == 0 and os.path.isfile(ffmpeg_output)
    print(f"  - สถานะการรัน: {'สำเร็จ (Code 0)' if res_ffmpeg.returncode == 0 else f'ล้มเหลว ({res_ffmpeg.returncode})'}")
    print(f"  - ใช้เวลาประมวลผล: {el_ffmpeg:.2f} วินาที")
    
    if ffmpeg_ok:
        noise_db_after_ff = measure_rms_db(ffmpeg_output, 0.2, 1.6)
        speech_db_after_ff = measure_rms_db(ffmpeg_output, 2.5, 3.0)
        noise_reduced_ff = noise_db_before - noise_db_after_ff
        print(f"  - [หลังตัด] ตัดเสียงรบกวนออกไปได้: {noise_reduced_ff:.2f} dB")
        t4_pass = noise_reduced_ff >= 2.0
    else:
        print(f"  - Error stderr: {res_ffmpeg.stderr}")
        t4_pass = False
    test_results.append(("FFmpeg afftdn Engine Processing & Noise Reduction", t4_pass))

    # -----------------------------------------------------------------
    # Test 5: ทดสอบการ Export หลากหลายฟอร์แมต (.mp3, .m4a) ลง Custom Folder
    # -----------------------------------------------------------------
    print("\n[TEST 5] ทดสอบ Export ฟอร์แมตต่าง ๆ (.mp3, .m4a) ลงโฟลเดอร์ปลายทาง")
    mp3_output = os.path.join(custom_save_dir, "result.mp3")
    m4a_output = os.path.join(custom_save_dir, "result.m4a")

    cmd_mp3 = [sys.executable, os.path.join(os.path.dirname(__file__), "denoise_video.py"),
               noisy_input_wav, mp3_output, "--reduction", "15"]
    cmd_m4a = [sys.executable, os.path.join(os.path.dirname(__file__), "denoise_video.py"),
               noisy_input_wav, m4a_output, "--reduction", "15"]

    r_mp3 = subprocess.run(cmd_mp3, capture_output=True)
    r_m4a = subprocess.run(cmd_m4a, capture_output=True)

    mp3_ok = r_mp3.returncode == 0 and os.path.isfile(mp3_output) and os.path.getsize(mp3_output) > 1000
    m4a_ok = r_m4a.returncode == 0 and os.path.isfile(m4a_output) and os.path.getsize(m4a_output) > 1000
    print(f"  - ส่งออกเป็น MP3: {'สำเร็จ' if mp3_ok else 'ล้มเหลว'} (ขนาด {os.path.getsize(mp3_output) if mp3_ok else 0} bytes)")
    print(f"  - ส่งออกเป็น M4A: {'สำเร็จ' if m4a_ok else 'ล้มเหลว'} (ขนาด {os.path.getsize(m4a_output) if m4a_ok else 0} bytes)")
    test_results.append(("Multi-format Export (MP3 & M4A)", mp3_ok and m4a_ok))

    # -----------------------------------------------------------------
    # สรุปผลการทดสอบ
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("📊 สรุปผลการทดสอบทั้งหมด (Benchmark Summary)")
    print("=" * 70)
    all_pass = True
    for name, p in test_results:
        status = "✅ PASS" if p else "❌ FAIL"
        if not p:
            all_pass = False
        print(f"  [{status}] {name}")
    print("=" * 70)

    # ล้างไฟล์ทดสอบชั่วคราว
    try:
        shutil.rmtree(sandbox_test_dir, ignore_errors=True)
    except Exception:
        pass

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(run_tests())
