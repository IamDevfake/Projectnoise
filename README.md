# ลดเสียงรบกวนในวิดีโอ (DenoiseVideo 1.1.0)

โปรแกรมลดเสียงรบกวน (พัดลม แอร์ เสียงฟู่ เสียงหึ่ง) ในวิดีโอหรือไฟล์เสียงยาว ๆ
ประมวลผลแบบสตรีม ใช้หน่วยความจำคงที่ ไม่ว่าไฟล์จะยาวเท่าไร ภาพจะถูกคัดลอกโดยไม่บีบอัดซ้ำ

## สิ่งที่ต้องมี
1. **Python 3.8 ขึ้นไป** (Windows: ติ๊ก “Add python.exe to PATH” ตอนติดตั้ง)
2. **ffmpeg** (มี ffprobe มาด้วย) — ติดตั้งให้เรียกใช้ได้จาก PATH **หรือ** วางไฟล์ `ffmpeg` / `ffprobe`
   ไว้ในโฟลเดอร์เดียวกับโปรแกรม (หรือโฟลเดอร์ย่อย `ffmpeg/bin`) ก็ใช้ได้เลย
3. ไลบรารี Python: `pip install -r requirements.txt`

## วิธีเปิด
- **Windows:** ดับเบิลคลิก `run_windows.bat` (ติดตั้ง numpy ให้เองถ้ายังไม่มี)
- **macOS / Linux:** `python3 denoise_gui.py`
- **บรรทัดคำสั่ง:** `python denoise_video.py input.mp4 output.mp4 --reduction 18 --highpass 80`

**คัดลอกไฟล์ `.py` ทั้งหมดไว้ในโฟลเดอร์เดียวกัน** (`denoise_gui.py`, `denoise_video.py`,
`denoise_spectral.py`, `denoise_common.py`)

## ถ้าใช้งานไม่ได้
กดปุ่ม **“ตรวจระบบ”** ในโปรแกรม (หรือรัน `python denoise_video.py --check`) จะบอกว่า Python, ffmpeg,
numpy, scipy เวอร์ชันอะไร และเอนจินไหนใช้ได้/ขาดอะไร กดคัดลอกผลแล้วส่งให้ผู้ดูแลได้
หลังอัปเดต Windows / Python / ffmpeg ให้รัน `python selftest.py` เพื่อทดสอบทั้งระบบอัตโนมัติ

| อาการ | วิธีแก้ |
|---|---|
| ไม่พบ ffmpeg | ติดตั้ง ffmpeg หรือวางไว้ข้างโปรแกรม แล้วกด “ตรวจระบบ” |
| numpy ติดตั้งอยู่แต่โหลดไม่ได้ | `pip install --upgrade numpy` (มักเกิดหลังอัปเดต Python) |
| เอนจิน Spectral เป็น “(ไม่พร้อม)” | ใช้เอนจิน FFmpeg ไปก่อน แล้วแก้ตามที่ “ตรวจระบบ” แจ้ง |
| ffmpeg ไม่มี filter/ตัวเข้ารหัส | อัปเดต ffmpeg เป็นรุ่นเต็ม (full build) |

## ไฟล์ตั้งค่า
เก็บที่ `%APPDATA%\DenoiseVideo` (Windows), `~/Library/Application Support/DenoiseVideo` (macOS),
`~/.config/DenoiseVideo` (Linux): `settings.json` (ค่าที่ใช้ล่าสุด) และ `presets.json` (พรีเซ็ตที่บันทึกเอง)
ลบไฟล์ได้เสมอเพื่อรีเซ็ต — ถ้าไฟล์เสียโปรแกรมจะเก็บสำเนาเป็น `.bak` แล้วเริ่มด้วยค่าเริ่มต้น
ตั้งค่าเองได้ด้วยตัวแปรสภาพแวดล้อม `DENOISE_CONFIG_DIR`, `DENOISE_FFMPEG`, `DENOISE_FFPROBE`

## สำหรับผู้พัฒนา: เพิ่มเอนจินใหม่
1. เพิ่มรายการใน `ENGINES` ของ `denoise_common.py` (ชื่อ, ไลบรารีที่ต้องมี, filter ffmpeg, ตัวเลือกที่รองรับ)
2. เขียนฟังก์ชันรันแล้วเพิ่มใน `RUNNERS` ของ `denoise_video.py` (รายงานความคืบหน้าผ่าน `rep.update(...)`)
3. รัน `python selftest.py` — หน้าจอจะสร้างปุ่มเลือกเอนจินและซ่อน/แสดงตัวเลื่อนให้เอง

ตัวเลือกใหม่ (ตัวเลื่อน) ก็เพิ่มที่ `OPTIONS` ที่เดียว