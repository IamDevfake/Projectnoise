"""เอนจิน spectral: ลด noise แบบสตรีม (หน่วยความจำคงที่) ต้องมี numpy; scipy ไม่บังคับ"""
import subprocess
import sys
import time

import numpy as np

import denoise_common as C

try:
    from scipy.ndimage import uniform_filter1d as _scipy_uniform
except Exception:  # noqa: BLE001  (ไม่มี scipy หรือ scipy รุ่นที่เข้ากันไม่ได้)
    _scipy_uniform = None

N_FFT = 2048
HOP = N_FFT // 4
# periodic sqrt-Hann: analysis * synthesis = Hann, overlap-add ที่ 75% รวมได้ 2.0
WIN = np.sqrt(np.hanning(N_FFT + 1)[:-1]).astype(np.float32)
OLA_SCALE = 0.5


# ---------------------------------------------------- ตัวกรองค่าเฉลี่ย (มี scipy หรือไม่ก็ได้)
def np_uniform_filter1d(x, size, axis):
    """เทียบเท่า scipy.ndimage.uniform_filter1d (mode='reflect') ด้วย numpy ล้วน"""
    x = np.moveaxis(np.asarray(x), axis, -1)
    left = size // 2
    right = size - 1 - left
    xp = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(left, right)], mode="symmetric")
    c = np.cumsum(xp, axis=-1, dtype=np.float64)
    c = np.concatenate([np.zeros(c.shape[:-1] + (1,)), c], axis=-1)
    out = (c[..., size:] - c[..., :-size]) / size
    return np.moveaxis(out.astype(x.dtype, copy=False), -1, axis)


def uniform_filter1d(x, size, axis):
    if _scipy_uniform is not None:
        return _scipy_uniform(x, size=size, axis=axis)
    return np_uniform_filter1d(x, size, axis)


# ----------------------------------------------------------------- ffmpeg decode
def decode_audio(path, sr, ch, highpass=0, start=None, dur=None, block_sec=10):
    """Generator: ส่งคืนเสียง float32 รูปร่าง (ch, n) ทีละบล็อก"""
    cmd = [C.find_tool("ffmpeg") or "ffmpeg", "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-i", path, "-vn", "-map", "0:a:0"]
    if highpass > 0:
        cmd += ["-af", f"highpass=f={highpass}"]
    cmd += ["-ac", str(ch), "-ar", str(sr), "-f", "f32le", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, **C.hide_console())
    bpf = 4 * ch
    blk = int(block_sec * sr) * bpf
    rem = b""
    try:
        while True:
            data = p.stdout.read(blk)
            if not data:
                break
            data = rem + data
            n = len(data) // bpf * bpf
            rem = data[n:]
            if n:
                yield np.frombuffer(data[:n], dtype=np.float32).reshape(-1, ch).T
    finally:
        p.stdout.close()
        p.wait()


# ------------------------------------------------------------- STFT helpers
def frames_of(x):
    """x: (ch, n) contiguous -> มุมมอง (ch, nframes, N_FFT) แบบไม่คัดลอกข้อมูล"""
    nf = (x.shape[1] - N_FFT) // HOP + 1
    s0, s1 = x.strides
    return np.lib.stride_tricks.as_strided(
        x, shape=(x.shape[0], nf, N_FFT), strides=(s0, s1 * HOP, s1))


def power_spec(frames):
    spec = np.fft.rfft(frames * WIN, axis=-1)
    return spec.real ** 2 + spec.imag ** 2


# ------------------------------------------------------------ noise profile
def estimate_noise(path, sr, ch, dur, highpass, noise_start, noise_dur, rep):
    """คืนค่า (noise power spectrum (ch, bins), noise_rms_db)"""
    # โหมดกำหนดช่วง noise เอง: เฉลี่ยทุกเฟรมในช่วงนั้น
    if noise_start is not None:
        buf = np.zeros((ch, 0), np.float32)
        for chunk in decode_audio(path, sr, ch, highpass, noise_start, noise_dur):
            buf = np.concatenate([buf, chunk], axis=1)
        if buf.shape[1] < N_FFT:
            sys.exit("ช่วง noise ที่ระบุสั้นเกินไป (ต้อง >= ~0.05 วินาที)")
        P = power_spec(frames_of(np.ascontiguousarray(buf)))
        rms = float(np.sqrt(np.mean(buf ** 2)))
        rms_db = 20 * np.log10(rms + 1e-20)
        return P.mean(axis=1), rms_db

    blk = sr  # 1 วินาที
    block_spec, block_energy, block_rms, has_zeros_list = [], [], [], []
    buf = np.zeros((ch, 0), np.float32)
    t0 = time.time()
    done = 0
    for chunk in decode_audio(path, sr, ch, highpass):
        buf = np.concatenate([buf, chunk], axis=1)
        while buf.shape[1] >= blk:
            seg = np.ascontiguousarray(buf[:, :blk])
            buf = buf[:, blk:]
            P = power_spec(frames_of(seg)[:, ::2])  # (ch, nf, bins)
            e = P.sum(axis=(0, 2))
            # ตรวจหาความเงียบสนิทดิจิทัล (zeros / mute / padding) เพื่อไม่ให้สับสนกับ ambient noise
            has_zeros = np.any(e < 1e-3)
            has_zeros_list.append(has_zeros)
            k = max(1, len(e) // 5)
            idx = np.argsort(e)[:k]                 # เฟรมที่เงียบสุด 20%
            block_spec.append(P[:, idx, :].mean(axis=1))
            block_energy.append(e[idx].mean())
            block_rms.append(float(np.sqrt(np.mean(seg ** 2))))
            done += 1
        if dur:
            rep.update(1, 2, done / dur * 100)
    rep.finish(1, 2, time.time() - t0)
    if not block_spec:
        sys.exit("ไฟล์เสียงสั้นเกินไปสำหรับประเมิน noise")

    # หลีกเลี่ยงบล็อกที่มี digital zeros (เช่น ช่วง fade-in / padding ต้นไฟล์)
    active = [i for i, h in enumerate(has_zeros_list) if not h]
    if active:
        sub = np.argsort([block_energy[i] for i in active])
        chosen = [active[i] for i in sub[:max(1, len(active) // 5)]]
    else:
        order = np.argsort(block_energy)
        chosen = order[:max(1, len(order) // 5)]

    noise_spec = np.mean([block_spec[i] for i in chosen], axis=0)
    noise_rms = float(np.mean([block_rms[i] for i in chosen]))
    noise_rms_db = 20 * np.log10(noise_rms + 1e-20)
    return noise_spec, noise_rms_db


# --------------------------------------------------------------- denoiser
class StreamingDenoiser:
    def __init__(self, noise_pow, ch, reduction_db, alpha, release, gate=1.0):
        self.ch = ch
        self.noise = noise_pow[:, None, :].astype(np.float32)
        self.floor = 10 ** (-reduction_db / 20.0)
        self.alpha = alpha
        self.release = release
        self.gate = float(gate)
        pre = N_FFT - HOP
        self.buf = np.zeros((ch, pre), np.float32)   # pre-pad ให้ตำแหน่งตรงกัน
        self.tail = np.zeros((ch, pre), np.float32)
        self.skip = pre
        self.prev_g = np.ones(N_FFT // 2 + 1, np.float32)
        self.real_in = 0
        self.emitted = 0

    def feed(self, x):
        self.real_in += x.shape[1]
        out = self._process(x)
        self.emitted += out.shape[1]
        return out

    def flush(self):
        out = self._process(np.zeros((self.ch, 2 * N_FFT), np.float32))
        return out[:, :max(0, self.real_in - self.emitted)]

    def _process(self, x):
        self.buf = np.concatenate([self.buf, x], axis=1)
        nf = (self.buf.shape[1] - N_FFT) // HOP + 1
        if nf <= 0:
            return np.zeros((self.ch, 0), np.float32)
        fr = frames_of(np.ascontiguousarray(self.buf))
        spec = np.fft.rfft(fr * WIN, axis=-1)
        P = spec.real ** 2 + spec.imag ** 2
        # ทำให้ค่าประมาณกำลังสัญญาณเรียบขึ้น (ลดความแกว่งของ noise -> ลด musical noise)
        P = uniform_filter1d(P, size=5, axis=2)
        if nf > 2:
            P = uniform_filter1d(P, size=3, axis=1)
        raw_g = np.sqrt(np.maximum(1.0 - self.alpha * self.noise / (P + 1e-20), 0.0))
        g = np.maximum(raw_g, self.floor).mean(axis=0)          # link ช่อง -> (nf, bins)
        g = uniform_filter1d(g, size=3, axis=1)
        prev, r = self.prev_g, self.release
        for i in range(nf):                                  # attack เร็ว / release ช้า
            cur = g[i]
            prev = np.where(cur > prev, cur, r * prev + (1 - r) * cur)
            g[i] = prev
        self.prev_g = prev
        spec *= g[None, :, :]
        out_fr = np.fft.irfft(spec, n=N_FFT, axis=-1) * WIN
        acc = np.zeros((self.ch, (nf - 1) * HOP + N_FFT), np.float32)
        acc[:, :self.tail.shape[1]] += self.tail
        for k in range(nf):
            acc[:, k * HOP:k * HOP + N_FFT] += out_fr[:, k]
        fin = acc[:, :nf * HOP] * OLA_SCALE
        self.tail = acc[:, nf * HOP:]
        self.buf = self.buf[:, nf * HOP:]
        if self.skip:
            d = min(self.skip, fin.shape[1])
            fin = fin[:, d:]
            self.skip -= d
        return fin


# ------------------------------------------------------------------ runner
def run_spectral(a, dur, sr, ch, has_video, tmp_err, rep):
    noise, noise_rms_db = estimate_noise(a.input, sr, ch, dur, a.highpass,
                                         a.noise_start, a.noise_dur, rep)
    gate_val = getattr(a, "gate", 1.0)
    den = StreamingDenoiser(noise, ch, a.reduction, a.alpha, a.release, gate=gate_val)

    # คำนวณตัวคูณ linear จาก volume (dB)
    vol_db = getattr(a, "volume", 0.0)
    vol_gain = 10.0 ** (vol_db / 20.0) if vol_db != 0.0 else 1.0

    # Pipeline: Python spectral denoise → ffmpeg [agate (soft expander) → volume → alimiter]
    # รัน agate ที่ระดับเสียงธรรมชาติต้นฉบับ (ก่อน volume boost) เพื่อไม่ให้ตัดคำพูด
    #
    # ⚠️ ปัญหาเสียงกระตุก (stutter): ratio=4 / attack=15ms ทำให้ agate ก้าวร้าวเกินไป
    #   - ช่วงพักระหว่างคำพูดมักมีระดับเสียงเพียง -30 ถึง -40 dB
    #   - threshold -22 dB ทับกับช่วงพักดังกล่าว → gate ปิด-เปิดบ่อย → กระตุก
    #
    # แนวทางแก้ไข:
    #   gate_val = 1.0 (ค่าปกติ): ไม่ใช้ agate เพราะ spectral denoiser กรอง noise แล้ว
    #   gate_val > 1.0 (ผู้ใช้ตั้งเอง): ใช้ agate แบบ "soft expander" เท่านั้น
    #     ratio=1.5 (ลดแทนที่จะตัด), knee=4.0 (เปลี่ยนผ่านนุ่มมาก)
    #     attack=30ms, release=400ms (เปิดเร็ว ปิดช้า ป้องกันเสียงแหว่ง)
    #     range=0.12 (ลดได้สูงสุด -12 dB แทนที่จะตัดทิ้ง)
    # alimiter ป้องกันเสียงแตกพร่า (distortion/clipping) เมื่อเร่ง volume สูง (+20 dB)
    af_parts = []
    if gate_val > 1.0 and C.has_filter("agate"):
        # Soft expander: ใช้เมื่อผู้ใช้ตั้ง gate > 1.0 เท่านั้น
        # คำนวณ threshold จาก noise floor: ยิ่ง gate สูง ยิ่ง threshold สูงขึ้น
        thr_db = min(-28.0, max(-55.0, noise_rms_db + 2.0 + (gate_val - 1.0) * 4.0))
        thr_lin = 10.0 ** (thr_db / 20.0)
        af_parts.append(
            f"agate=threshold={thr_lin:.6f}:ratio=1.5:knee=4.0:attack=30:release=400:range=0.12"
        )
    if vol_db != 0.0:
        af_parts.append(f"volume={vol_db}dB")
    if vol_db > 0.0 and C.has_filter("alimiter"):
        af_parts.append("alimiter=limit=0.95:level=0:attack=5:release=50:asc=1")

    cmd = [C.find_tool("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-nostdin", "-i", a.input,
           "-f", "f32le", "-ar", str(sr), "-ac", str(ch),
           "-thread_queue_size", "1024", "-i", "pipe:0",
           "-map", "0:v:0?", "-map", "1:a", "-map_metadata", "0"]
    cmd += C.video_args(has_video, a.video_denoise)
    cmd += C.audio_args(a.output, a.audio_bitrate)
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    cmd += ["-max_muxing_queue_size", "9999"] + C.container_flags(a.output)
    cmd += [a.output]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=tmp_err, **C.hide_console())

    t0, done = time.time(), 0
    try:
        for chunk in decode_audio(a.input, sr, ch, a.highpass):
            out = den.feed(np.ascontiguousarray(chunk))
            enc.stdin.write(np.ascontiguousarray(out.T).tobytes())
            done += chunk.shape[1]
            pct = min(100.0, done / sr / dur * 100) if dur else 0
            el = time.time() - t0
            eta = el / pct * (100 - pct) if pct > 0 else 0
            rep.update(2, 2, pct, eta)
        flush_out = den.flush()
        enc.stdin.write(np.ascontiguousarray(flush_out.T).tobytes())
        enc.stdin.close()
    except BrokenPipeError:
        pass
    rc = enc.wait()
    rep.finish(2, 2, time.time() - t0)
    return rc