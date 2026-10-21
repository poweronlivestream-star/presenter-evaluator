import os
import sys
import time
import json
import base64
import glob
import subprocess
from datetime import datetime
from email.mime.text import MIMEText
import requests

FOLDER_ID = os.environ["FOLDER_ID"]
CLEAN_NAME = os.environ["CLEAN_NAME"]
LOG_FILE_ID = os.environ["LOG_FILE_ID"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
DRIVE_TOKEN = os.environ["DRIVE_TOKEN"]
API_KEYS = [
    k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()
]

START_TIME = time.time()
LOG_LINES = []


def log_step(msg):
  timestamp = datetime.utcnow().strftime("%H:%M:%S UTC")
  line = f"[{timestamp}] {msg}"
  print(line, flush=True)
  LOG_LINES.append(line)
  try:
    headers = {
        "Authorization": f"Bearer {DRIVE_TOKEN}",
        "Content-Type": "text/plain; charset=utf-8",
    }
    requests.patch(
        f"https://www.googleapis.com/upload/drive/v3/files/{LOG_FILE_ID}?uploadType=media",
        headers=headers,
        data="\n".join(LOG_LINES).encode("utf-8"),
        timeout=15,
    )
  except Exception as e:
    print(f"تحذير تحديث اللوغ: {e}", flush=True)


def send_gmail(subject, body):
  msg = MIMEText(body, "plain", "utf-8")
  msg["to"] = NOTIFY_EMAIL
  msg["subject"] = subject
  raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
  headers = {
      "Authorization": f"Bearer {DRIVE_TOKEN}",
      "Content-Type": "application/json",
  }
  requests.post(
      "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
      headers=headers,
      json={"raw": raw_msg},
      timeout=20,
  )


def rename_drive_folder(new_name):
  headers = {
      "Authorization": f"Bearer {DRIVE_TOKEN}",
      "Content-Type": "application/json",
  }
  requests.patch(
      f"https://www.googleapis.com/drive/v3/files/{FOLDER_ID}",
      headers=headers,
      json={"name": new_name},
      timeout=15,
  )


def upload_report_to_drive(filename, html_content):
  metadata = {"name": filename, "parents": [FOLDER_ID], "mimeType": "text/html"}
  boundary = "===boundary_report==="
  body = (
      f"--{boundary}\r\n"
      "Content-Type: application/json; charset=UTF-8\r\n\r\n"
      f"{json.dumps(metadata)}\r\n"
      f"--{boundary}\r\n"
      "Content-Type: text/html; charset=UTF-8\r\n\r\n"
      f"{html_content}\r\n"
      f"--{boundary}--"
  ).encode("utf-8")
  headers = {
      "Authorization": f"Bearer {DRIVE_TOKEN}",
      "Content-Type": f"multipart/related; boundary={boundary}",
  }
  requests.post(
      "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
      headers=headers,
      data=body,
      timeout=30,
  )


def list_drive_media_files():
  headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
  q = f"'{FOLDER_ID}' in parents and trashed = false"
  res = requests.get(
      "https://www.googleapis.com/drive/v3/files",
      headers=headers,
      params={"q": q, "fields": "files(id, name, mimeType, size)"},
      timeout=20,
  )
  res.raise_for_status()
  files = res.json().get("files", [])
  media = [
      f
      for f in files
      if f["mimeType"].startswith("video/")
      or f["mimeType"].startswith("audio/")
  ]
  media.sort(key=lambda x: x["name"])
  return media


def download_drive_file(file_id, local_path):
  headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
  with requests.get(
      f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
      headers=headers,
      stream=True,
      timeout=120,
  ) as r:
    r.raise_for_status()
    with open(local_path, "wb") as f:
      for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
        if chunk:
          f.write(chunk)


def get_duration_seconds(filepath):
  cmd = [
      "ffprobe",
      "-v",
      "error",
      "-show_entries",
      "format=duration",
      "-of",
      "default=noprint_wrappers=1:nokey=1",
      filepath,
  ]
  out = subprocess.check_output(cmd).decode("utf-8").strip()
  return float(out)


def format_hhmmss(seconds):
  s = int(round(seconds))
  return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def prepare_media_segments(drive_files):
  # إذا رفع العميل ملف فيديو خام واحد، نقوم بتشغيل FFmpeg تلقائياً لفصل الصوت والصورة وتقسيمهما
  if len(drive_files) == 1 and drive_files[0]["mimeType"].startswith("video/"):
    raw_file = drive_files[0]
    size_mb = int(raw_file.get("size", 0)) / (1024 * 1024)
    log_step(
        f"جاري سحب ملف الفيديو الخام ({raw_file['name']} - {size_mb:.1f} MB) من"
        " Google Drive..."
    )
    local_raw = "raw_episode.mp4"
    download_drive_file(raw_file["id"], local_raw)

    total_sec = get_duration_seconds(local_raw)
    exact_duration = format_hhmmss(total_sec)
    log_step(
        f"اكتمل سحب الفيديو. المدة الفعلية المرصودة عبر ffprobe:"
        f" {exact_duration}"
    )

    log_step(
        "جاري تشغيل FFmpeg لاستخراج وتقسيم الصوت (MP3 64kbps) كل 25 دقيقة..."
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            local_raw,
            "-vn",
            "-c:a",
            "libmp3lame",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            "1500",
            "-reset_timestamps",
            "1",
            "Audio_part%02d.mp3",
        ],
        check=True,
    )

    log_step(
        "جاري تشغيل FFmpeg لاستخراج وتقسيم الصورة (MP4 1fps 720p) كل 25"
        " دقيقة..."
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            local_raw,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "faster",
            "-crf",
            "30",
            "-maxrate",
            "150k",
            "-bufsize",
            "300k",
            "-vf",
            "scale=-2:720,fps=1",
            "-f",
            "segment",
            "-segment_time",
            "1500",
            "-reset_timestamps",
            "1",
            "Visuals_part%02d.mp4",
        ],
        check=True,
    )

    segments = sorted(glob.glob("Audio_part*.mp3")) + sorted(
        glob.glob("Visuals_part*.mp4")
    )
    return segments, exact_duration
  else:
    # في حال كانت الملفات مقسمة مسبقاً
    local_files = []
    total_audio_sec = 0.0
    for f in drive_files:
      log_step(f"جاري سحب الملف ({f['name']})...")
      download_drive_file(f["id"], f["name"])
      local_files.append(f["name"])
      if f["mimeType"].startswith("audio/"):
        total_audio_sec += get_duration_seconds(f["name"])
    exact_duration = (
        format_hhmmss(total_audio_sec) if total_audio_sec > 0 else "غير محدد"
    )
    return local_files, exact_duration


def upload_to_gemini_file_api(filepath, api_key):
  mime = "audio/mp3" if filepath.endswith(".mp3") else "video/mp4"
  filesize = os.path.getsize(filepath)
  init_url = (
      f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}"
  )
  init_headers = {
      "X-Goog-Upload-Protocol": "resumable",
      "X-Goog-Upload-Command": "start",
      "X-Goog-Upload-Header-Content-Length": str(filesize),
      "X-Goog-Upload-Header-Content-Type": mime,
      "Content-Type": "application/json",
  }
  r = requests.post(
      init_url,
      headers=init_headers,
      json={"file": {"display_name": filepath}},
      timeout=30,
  )
  r.raise_for_status()
  upload_url = r.headers["X-Goog-Upload-URL"]

  with open(filepath, "rb") as f:
    up_res = requests.post(
        upload_url,
        headers={
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        data=f,
        timeout=300,
    )
  up_res.raise_for_status()
  file_info = up_res.json()["file"]

  # انتظار جهوزية الملف ACTIVE
  for _ in range(60):
    if file_info.get("state") == "ACTIVE":
      return file_info["uri"], mime
    time.sleep(3)
    chk = requests.get(f"{file_info['uri']}?key={api_key}", timeout=20).json()
    file_info["state"] = chk.get("state")
  raise RuntimeError(f"لم يصبح الملف {filepath} في حالة ACTIVE.")


def wrap_word_html(title, body_content):
  return f"""<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office"
      xmlns:w="urn:schemas-microsoft-com:office:word"
      xmlns="http://www.w3.org/TR/REC-html40"
      dir="rtl" lang="ar">
<head>
<meta charset="utf-8">
<title>{title}</title>
<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View><w:Zoom>100</w:Zoom><w:DoNotOptimizeForBrowser/></w:WordDocument></xml><![endif]-->
<style>
  @page {{ size: A4; margin: 2cm; }}
  body {{ font-family: "Arial", sans-serif; font-size: 12pt; line-height: 1.5; direction: rtl; text-align: right; color: #111111; }}
  .thinking-note {{ font-size: 8.5pt; color: #666666; text-align: left; border-bottom: 1px dotted #cccccc; padding-bottom: 3px; margin-bottom: 10px; }}
  h1 {{ text-align: center; font-size: 18pt; font-weight: bold; color: #1F4E79; margin-bottom: 4px; page-break-after: avoid; }}
  .subtitle {{ text-align: center; font-size: 13pt; font-weight: bold; color: #595959; margin-bottom: 18px; page-break-after: avoid; }}
  h2 {{ font-size: 14pt; font-weight: bold; color: #1F4E79; margin-top: 18px; margin-bottom: 8px; border-bottom: 1.5px solid #1F4E79; padding-bottom: 4px; page-break-after: avoid; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; page-break-inside: avoid; }}
  table, th, td {{ border: 1px solid #444444; }}
  th, td {{ padding: 7px 10px; text-align: right; vertical-align: top; page-break-inside: avoid; }}
  tr {{ page-break-inside: avoid; page-break-after: auto; }}
  th {{ background-color: #D9E1F2; color: #1F4E79; font-weight: bold; }}
  .info-table td:first-child {{ font-weight: bold; background-color: #F2F2F2; width: 22%; }}
  .summary-strip td {{ background-color: #F9FAFB; text-align: center; width: 25%; }}
  .core-grid td {{ width: 33.33%; background-color: #F9F9F9; font-weight: bold; }}
  ul {{ margin-top: 6px; margin-bottom: 14px; padding-right: 22px; }}
  li {{ margin-bottom: 6px; page-break-inside: avoid; }}
</style>
</head>
<body>
{body_content}
</body>
</html>"""


def evaluate_with_gemini(segments, exact_duration):
  models = [
      (
          "gemini-3.1-pro-preview",
          24576,
          "gemini-3.1-pro-preview (Max Thinking 24576)",
          True,
      ),
      (
          "gemini-3.1-pro-preview",
          8192,
          "gemini-3.1-pro-preview (Med Thinking 8192)",
          False,
      ),
      (
          "gemini-3.8-flash",
          24576,
          "gemini-3.8-flash (High Thinking 24576)",
          False,
      ),
      ("gemini-3.8-flash", 0, "gemini-3.8-flash (Standard Fast)", False),
  ]

  system_prompt = (
      "أنت خبير تقييم أداء مقدمي البرامج التلفزيونية وفق السلم المعتمد (90"
      " نقطة).\nقواعد إلزامية صارمة:\n1. المنع المطلق للتخمين (Zero"
      " Hallucination): اعتمد حصرياً وبنسبة 100% على محتوى ملفات الصوت والفيديو"
      " المرفقة لهذه الحلقة فقط. يُمنع منعاً باتاً نقل أي اسم أو معلومة من أي"
      " نموذج سابق. أي معلومة غير مذكورة صراحة يُكتب مكانها 'غير محدد في"
      " المقطع'.\n2. التحقق من كامل المدة: المدة الفعلية للحلقة المقاسة عبر"
      f" ffprobe هي ({exact_duration}). يجب تحليل الحلقة بالكامل من الثانية"
      " 00:00 حتى آخر ثانية.\n3. سلم العلامات (90 نقطة): إدارة الحلقة (20)،"
      " التعامل مع الضيف (20)، جودة الأسئلة (20)، المتابعة والتعقيب (15)،"
      " الحضور والأداء (10)، ضبط الوقت (5).\n4. أرجع كائن JSON حصراً بالحقول"
      " التالية:\n   - isFullDurationAnalyzed: (boolean)\n   -"
      " detectedTotalDuration: (string)\n   - lastAnalyzedTimestamp:"
      " (string)\n   - failureReason: (string)\n   - mainDetailedHtml: (string)"
      " كود HTML للتقرير الرئيسي المفصل وفق المعايير الموسعة شاملاً:"
      " <h1>تقييم تفصيلي وفق المعايير الموسعة</h1>، العنوان الفرعي، جدول"
      " البيانات الأساسية (class='info-table')، الخلاصة التنفيذية، الشريط"
      " الرباعي (class='summary-strip')، منهج التقييم، شبكة العلامات الأساسية"
      " (class='core-grid')، الجداول التفصيلية الـ 6 كافة مع معاييرها الفرعية"
      " الـ 23، جدول 'أمثلة موثقة من الحلقة' بالتوقيت (من 00:00 إلى 00:00)"
      " موزعاً على البداية والوسط والنهاية، نقاط القوة الرئيسية، أولويات"
      " التطوير، والخلاصة النهائية.\n   - summarizedHtml: (string) كود HTML"
      " للتقرير المختصر شاملاً: <h1>نموذج تقييم أداء مقدم/ة برنامج (تقرير"
      " مختصر)</h1>، جدول البيانات الأساسية (class='info-table')، الخلاصة"
      " التنفيذية، جدول النتيجة الإجمالية، جدول العلامات الأساسية الستة مع صف"
      " المجموع الكلي من 90، نقاط القوة الرئيسية، وأولويات التطوير."
  )

  errors = []
  for key_idx, api_key in enumerate(API_KEYS, start=1):
    log_step(f"فحص صلاحية وحصة المفتاح Key #{key_idx}...")
    # رفع الأجزاء على المفتاح الحالي
    try:
      prompt_parts = []
      for seg in segments:
        log_step(f"رفع الجزء ({seg}) إلى Gemini باستخدام Key #{key_idx}...")
        uri, mime = upload_to_gemini_file_api(seg, api_key)
        prompt_parts.append({"text": f"الملف المرفق التالي هو: {seg}"})
        prompt_parts.append({"fileData": {"mimeType": mime, "fileUri": uri}})
    except Exception as up_err:
      errors.append(f"Key #{key_idx} Upload Error: {up_err}")
      continue

    for model_name, budget, label, is_highest in models:
      log_step(f"بدء التحليل العميق عبر النموذج: {label} (Key #{key_idx})...")
      gen_cfg = {"temperature": 0.0, "responseMimeType": "application/json"}
      if budget > 0:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}

      payload = {
          "systemInstruction": {"parts": [{"text": system_prompt}]},
          "contents": [{
              "role": "user",
              "parts": prompt_parts + [{
                  "text": (
                      f"حلل وقيّم حلقة ({CLEAN_NAME}) بالكامل بدقة تامة دون أي"
                      " تخمين."
                  )
              }],
          }],
          "generationConfig": gen_cfg,
      }
      url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

      for attempt in range(1, 4):
        res = requests.post(url, json=payload, timeout=600)
        if res.status_code == 200:
          data = res.json()
          text = data["candidates"][0]["content"]["parts"][0]["text"]
          parsed = json.loads(text)
          banner = (
              ""
              if is_highest
              else (
                  '<div class="thinking-note">مستوى التفكير/النموذج المستخدم:'
                  f" {label} (Key #{key_idx})</div>"
              )
          )
          parsed["thinkingBanner"] = banner
          parsed["levelUsed"] = f"{label} (Key #{key_idx})"
          return parsed
        elif res.status_code in (503, 429):
          log_step(
              f"تنبيه ({res.status_code}) على {label} - محاولة {attempt}/3..."
          )
          time.sleep(8 * attempt)
        else:
          errors.append(
              f"Key #{key_idx} [{label}] HTTP {res.status_code}: {res.text}"
          )
          break

  raise RuntimeError(
      "فشلت جميع المفاتيح والنماذج المتاحة:\n" + "\n".join(errors)
  )


def main():
  try:
    log_step(f"انطلاق معالجة الحلقة: {CLEAN_NAME}")
    drive_files = list_drive_media_files()
    if not drive_files:
      raise RuntimeError("المجلد لا يحتوي على أي ملفات فيديو أو صوت.")

    segments, exact_duration = prepare_media_segments(drive_files)
    result = evaluate_with_gemini(segments, exact_duration)

    if not result.get("isFullDurationAnalyzed"):
      raise RuntimeError(
          f"لم يكتمل تحليل كامل المدة. المدة الفعلية: {exact_duration} | آخر"
          f" توقيت محلل: {result.get('lastAnalyzedTimestamp')} | السبب:"
          f" {result.get('failureReason')}"
      )

    log_step("اكتمل التقييم بنجاح! جاري بناء وحفظ ملفات Word وHTML على Drive...")
    main_html = wrap_word_html(
        f"تقييم تفصيلي وفق المعايير الموسعة - {CLEAN_NAME}",
        result["thinkingBanner"] + result["mainDetailedHtml"],
    )
    sum_html = wrap_word_html(
        f"تقرير تقييم مختصر - {CLEAN_NAME}",
        result["thinkingBanner"] + result["summarizedHtml"],
    )

    upload_report_to_drive(
        f"01_Main_Detailed_Report_{CLEAN_NAME}.html", main_html
    )
    upload_report_to_drive(
        f"01_Main_Detailed_Report_{CLEAN_NAME}.doc", main_html
    )
    upload_report_to_drive(f"02_Summarized_Report_{CLEAN_NAME}.html", sum_html)
    upload_report_to_drive(f"02_Summarized_Report_{CLEAN_NAME}.doc", sum_html)

    elapsed = int(time.time() - START_TIME)
    elapsed_str = f"{elapsed // 60} دقيقة و {elapsed % 60} ثانية"
    log_step(f"تمت العملية بالكامل بنجاح في {elapsed_str}!")
    rename_drive_folder(f"PROCESSED_{CLEAN_NAME}")

    folder_url = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
    send_gmail(
        f"✅ اكتمل تقييم حلقة ({CLEAN_NAME}) بنجاح",
        f"تم الانتهاء من تقسيم وتحليل وتقييم حلقة ({CLEAN_NAME}) وإصدار"
        " التقريرين (المفصل والمختصر).\n\n📊 ملخص"
        f" العملية:\n----------------------------------------\n• اسم الحلقة:"
        f" {CLEAN_NAME}\n• المدة الفعلية للحلقة (ffprobe): {exact_duration}\n•"
        f" آخر توقيت تم تحليله: {result.get('lastAnalyzedTimestamp')} (تغطية"
        " 100%)\n• النموذج والمفتاح المستخدم:"
        f" {result.get('levelUsed')}\n• الوقت المستغرق الكلي:"
        " {elapsed_str}\n----------------------------------------\n\n📁 رابط"
        f" المجلد والتقارير على Google Drive:\n{folder_url}",
    )
  except Exception as e:
    log_step(f"❌ خطأ قاتل: {e}")
    rename_drive_folder(f"FAILED_{CLEAN_NAME}")
    folder_url = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
    send_gmail(
        f"❌ فشل تقييم الحلقة: {CLEAN_NAME}",
        f"تعذر إتمام تقييم حلقة ({CLEAN_NAME}).\n\n⚠️ السبب الدقيق"
        f" للفشل:\n----------------------------------------\n{e}\n----------------------------------------\n\n📁"
        " رابط المجلد وسجل اللوغ"
        f" (00_LIVE_LOG.txt):\n{folder_url}",
    )
    sys.exit(1)


if __name__ == "__main__":
  main()
