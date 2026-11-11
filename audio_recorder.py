"""โมดูลบันทึกเสียง (Audio Recorder) สำหรับอัดเสียงสดจากไมโครโฟน
รองรับการเริ่ม/หยุดบันทึก, ตรวจจับระดับเสียง (Peak/RMS Level สำหรับ VU Meter),
และบันทึกเป็นไฟล์ WAV เพื่อส่งต่อเข้าสู่กระบวนการลดเสียงรบกวนหรือบันทึกลงโฟลเดอร์
"""
import os
import queue
import threading
import time
import wave
import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None


class AudioRecorder:
    def __init__(self, sample_rate=44100, channels=1):
        self.sample_rate = sample_rate
        self.channels = channels
        self.is_recording = False
        self.is_paused = False
        self._frames = []
        self._stream = None
        self._lock = threading.Lock()
        self._start_time = 0.0
        self._recorded_seconds = 0.0
        self.current_peak = 0.0
        self.current_rms = 0.0

    @staticmethod
    def is_available():
        if sd is None:
            return False, "ยังไม่ได้ติดตั้งไลบรารี sounddevice"
        try:
            devs = sd.query_devices()
            inputs = [d for d in devs if d.get("max_input_channels", 0) > 0]
            if not inputs:
                return False, "ไม่พบอุปกรณ์ไมโครโฟน (Audio Input Device)"
            return True, "พร้อมใช้งาน"
        except Exception as e:
            return False, f"ข้อผิดพลาด sounddevice: {e}"

    @staticmethod
    def get_input_devices():
        if sd is None:
            return []
        try:
            devices = []
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0:
                    devices.append((idx, dev.get("name", f"Device {idx}")))
            return devices
        except Exception:
            return []

    def _audio_callback(self, indata, frames, time_info, status):
        if not self.is_recording or self.is_paused:
            return
        # คัดลอกข้อมูลเสียง
        data_copy = indata.copy()
        with self._lock:
            self._frames.append(data_copy)
            # คำนวณค่า Peak และ RMS สำหรับ VU Meter
            peak = float(np.max(np.abs(data_copy)))
            rms = float(np.sqrt(np.mean(data_copy ** 2)))
            self.current_peak = peak
            self.current_rms = rms

    def start_recording(self, device_index=None):
        if self.is_recording:
            return False, "กำลังบันทึกเสียงอยู่แล้ว"
        
        ok, reason = self.is_available()
        if not ok:
            return False, reason

        with self._lock:
            self._frames.clear()
            self.current_peak = 0.0
            self.current_rms = 0.0
            self._start_time = time.time()
            self._recorded_seconds = 0.0
            self.is_recording = True
            self.is_paused = False

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                device=device_index,
                callback=self._audio_callback,
                blocksize=1024,
            )
            self._stream.start()
            return True, "เริ่มบันทึกเสียงเรียบร้อย"
        except Exception as e:
            self.is_recording = False
            return False, f"ไม่สามารถเปิดไมโครโฟนได้: {e}"

    def pause_recording(self):
        if not self.is_recording:
            return
        self.is_paused = not self.is_paused

    def get_duration(self):
        if not self.is_recording:
            return self._recorded_seconds
        if self.is_paused:
            return self._recorded_seconds
        with self._lock:
            total_samples = sum(len(f) for f in self._frames)
            return total_samples / self.sample_rate

    def get_levels(self):
        """คืนค่า (peak, rms) ช่วง 0.0 - 1.0"""
        with self._lock:
            return self.current_peak, self.current_rms

    def stop_recording(self):
        if not self.is_recording and self._stream is None:
            return False, "ไม่ได้กำลังบันทึกเสียง"

        self.is_recording = False
        self.is_paused = False

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        with self._lock:
            if not self._frames:
                return False, "ไม่มีข้อมูลเสียงที่บันทึก"
            total_samples = sum(len(f) for f in self._frames)
            self._recorded_seconds = total_samples / self.sample_rate

        return True, f"บันทึกเสียงสำเร็จ ความยาว {self._recorded_seconds:.1f} วินาที"

    def get_audio_data(self):
        with self._lock:
            if not self._frames:
                return None
            data = np.concatenate(self._frames, axis=0)
            return data

    def save_to_file(self, output_path):
        """บันทึกข้อมูลเสียงที่อัดเป็นไฟล์ WAV 16-bit PCM"""
        with self._lock:
            if not self._frames:
                return False, "ไม่มีข้อมูลเสียงที่จะบันทึก"
            audio_data = np.concatenate(self._frames, axis=0)

        # บีบอัดเป็น 16-bit PCM
        audio_int16 = np.int16(np.clip(audio_data, -1.0, 1.0) * 32767)

        # สร้างโฟลเดอร์หากยังไม่มี
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_int16.tobytes())

        return True, f"บันทึกไฟล์เรียบร้อยที่: {output_path}"
