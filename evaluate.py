import os
import sys
import time
import json
import glob
import subprocess
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import html

FOLDER_ID = os.environ["FOLDER_ID"]
CLEAN_NAME = os.environ["CLEAN_NAME"]
LOG_FILE_ID = os.environ["LOG_FILE_ID"]
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")
DRIVE_TOKEN = os.environ["DRIVE_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

API_KEYS = [
    k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()
]

START_TIME = time.time()
LOG_LINES = []

# جلسة الرفع لـ Gemini (مخصصة للملفات الثقيلة مع إعادة محاولة)
session = requests.Session()
retries = Retry(total=10, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
session.mount('https://', HTTPAdapter(max_retries=retries))

def send_telegram(message):
    """
    إرسال تيليغرام باستخدام تنسيق HTML بدلاً من Markdown
    لضمان عدم انهيار الرسالة إذا احتوت على رموز برمجية أو أقواس غير مغلقة.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        # استخدام requests عادي بدلاً من session لتفادي تداخل مهلات الرفع
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code != 200:
            print(f"Telegram Formatting Error: {res.text}. Retrying as plain text...", flush=True)
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"خطأ في إرسال تليغرام: {e}", flush=True)

def log_step(msg):
    timestamp = datetime.utcnow().strftime("%H:%M:%S UTC")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)
    
    # حماية النص من كسر الـ HTML
    safe_msg = html.escape(msg[:300])
    send_telegram(f"🔔 <b>تحديث معالجة:</b> <code>{CLEAN_NAME}</code>\n<i>{safe_msg}</i>")
    
    try:
        headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "text/plain; charset=utf-8"}
        session.patch(f"https://www.googleapis.com/upload/drive/v3/files/{LOG_FILE_ID}?uploadType=media", headers=headers, data="\n".join(LOG_LINES).encode("utf-8"), timeout=20)
    except Exception as e:
        print(f"تحذير تحديث اللوغ: {e}", flush=True)

def update_drive_folder(new_name, description_text=""):
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json"}
    payload = {"name": new_name}
    if description_text:
        payload["description"] = description_text
    session.patch(f"https://www.googleapis.com/drive/v3/files/{FOLDER_ID}", headers=headers, json=payload, timeout=20)

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
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"multipart/related; boundary={boundary}"}
    session.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart", headers=headers, data=body, timeout=40)

def list_drive_media_files():
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    q = f"'{FOLDER_ID}' in parents and trashed = false"
    res = session.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q, "fields": "files(id, name, mimeType, size)"}, timeout=30)
    res.raise_for_status()
    files = res.json().get("files", [])
    media = [f for f in files if f["mimeType"].startswith("video/") or f["mimeType"].startswith("audio/")]
    media.sort(key=lambda x: x["name"])
    return media

def download_drive_file(file_id, local_path):
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    with session.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk: f.write(chunk)

def get_duration_seconds(filepath):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    out = subprocess.check_output(cmd).decode("utf-8").strip()
    return float(out)

def format_hhmmss(seconds):
    s = int(round(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

def prepare_media_segments(drive_files):
    if len(drive_files) == 1 and drive_files[0]["mimeType"].startswith("video/"):
        raw_file = drive_files[0]
        size_mb = int(raw_file.get("size", 0)) / (1024 * 1024)
        log_step(f"📥 جاري سحب ملف الفيديو الخام ({raw_file['name']} - {size_mb:.1f} MB) من Google Drive...")
        local_raw = "raw_episode.mp4"
        download_drive_file(raw_file["id"], local_raw)
        total_sec = get_duration_seconds(local_raw)
        exact_duration = format_hhmmss(total_sec)
        log_step(f"⏱️ المدة الفعلية للحلقة المرصودة عبر ffprobe: {exact_duration}")
        
        log_step("✂️ جاري تشغيل FFmpeg لاستخراج وتقسيم الصوت (MP3) كل 25 دقيقة...")
        subprocess.run(["ffmpeg", "-y", "-i", local_raw, "-vn", "-c:a", "libmp3lame", "-ac", "1", "-ar", "24000", "-b:a", "64k", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Audio_part%02d.mp3"], check=True)
        
        log_step("🎞️ جاري تشغيل FFmpeg لاستخراج وتقسيم الصورة (فيديو خفيف) كل 25 دقيقة...")
        subprocess.run(["ffmpeg", "-y", "-i", local_raw, "-an", "-c:v", "libx264", "-preset", "faster", "-crf", "30", "-maxrate", "150k", "-bufsize", "300k", "-vf", "scale=-2:720,fps=1", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Visuals_part%02d.mp4"], check=True)
        
        segments = sorted(glob.glob("Audio_part*.mp3")) + sorted(glob.glob("Visuals_part*.mp4"))
        return segments, exact_duration
    else:
        local_files = []
        total_audio_sec = 0.0
        for f in drive_files:
            log_step(f"📥 جاري سحب الملف المقسم ({f['name']})...")
            download_drive_file(f["id"], f["name"])
            local_files.append(f["name"])
            if f["mimeType"].startswith("audio/"):
                total_audio_sec += get_duration_seconds(f["name"])
        exact_duration = format_hhmmss(total_audio_sec) if total_audio_sec > 0 else "غير محدد"
        return local_files, exact_duration

def upload_to_gemini_file_api(filepath, api_key):
    """
    نسخة احترافية فولاذية (Stable Max) لرفع الملفات إلى Gemini API
    تقوم برفع الملف على شكل حزم (Chunks) صغيرة (8MB) لتجنب انقطاع الاتصال المفاجئ
    مع قدرة على استئناف الرفع (Resumable) إذا انقطع الاتصال في المنتصف.
    """
    mime = "audio/mp3" if filepath.endswith(".mp3") else "video/mp4"
    filesize = os.path.getsize(filepath)
    
    init_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}"
    init_headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(filesize),
        "X-Goog-Upload-Header-Content-Type": mime,
        "Content-Type": "application/json"
    }
    r = session.post(init_url, headers=init_headers, json={"file": {"display_name": filepath}}, timeout=30)
    r.raise_for_status()
    upload_url = r.headers["X-Goog-Upload-URL"]

    chunk_size = 8 * 1024 * 1024
    offset = 0
    
    with open(filepath, "rb") as f:
        while offset < filesize:
            chunk = f.read(chunk_size)
            is_final = (offset + len(chunk) >= filesize)
            command = "upload, finalize" if is_final else "upload"
            
            chunk_headers = {
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": command,
                "X-Goog-Upload-Offset": str(offset),
                "Content-Length": str(len(chunk))
            }
            
            up_res = session.post(upload_url, headers=chunk_headers, data=chunk, timeout=120)
            up_res.raise_for_status()
            offset += len(chunk)

    file_info = up_res.json()["file"]

    for _ in range(60):
        if file_info.get("state") == "ACTIVE":
            return file_info["uri"], mime
        time.sleep(3)
        chk = session.get(f"{file_info['uri']}?key={api_key}", timeout=20).json()
        file_info["state"] = chk.get("state")
    raise RuntimeError(f"لم يصبح الملف {filepath} في حالة ACTIVE بعد الرفع.")

def wrap_html(title, body_content):
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: "Arial", sans-serif; font-size: 14pt; line-height: 1.6; direction: rtl; text-align: right; color: #222222; max-width: 900px; margin: 40px auto; padding: 20px; background-color: #fdfdfd; }}
  .thinking-note {{ font-size: 9pt; color: #666666; text-align: left; border-bottom: 1px dotted #cccccc; padding-bottom: 4px; margin-bottom: 15px; }}
  h1 {{ text-align: center; font-size: 20pt; font-weight: bold; color: #1F4E79; margin-bottom: 6px; }}
  h2 {{ font-size: 15pt; font-weight: bold; color: #1F4E79; margin-top: 22px; margin-bottom: 10px; border-bottom: 2px solid #1F4E79; padding-bottom: 5px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
  table, th, td {{ border: 1px solid #555555; }}
  th, td {{ padding: 10px 12px; text-align: right; vertical-align: top; }}
  th {{ background-color: #D9E1F2; color: #1F4E79; font-weight: bold; }}
  ul {{ margin-top: 8px; margin-bottom: 16px; padding-right: 25px; }}
  li {{ margin-bottom: 8px; }}
</style>
</head>
<body>{body_content}</body>
</html>"""

def evaluate_with_gemini(segments, exact_duration, total_segments_count):
    models = [
        ("gemini-3.1-pro-preview", 24576, "gemini-3.1-pro-preview (Max Thinking 24576)", True),
        ("gemini-3.1-pro-preview", 8192,  "gemini-3.1-pro-preview (Med Thinking 8192)",  False),
        ("gemini-3.8-flash",       24576, "gemini-3.8-flash (High Thinking 24576)",      False),
        ("gemini-3.8-flash",       0,     "gemini-3.8-flash (Standard Fast)",            False)
    ]
    
    audio_segs = sorted(glob.glob("Audio_part*.mp3"))
    seg_timeline_info = []
    accum_sec = 0.0
    for idx, aseg in enumerate(audio_segs):
        dur = get_duration_seconds(aseg)
        start_str = format_hhmmss(accum_sec)
        end_str = format_hhmmss(accum_sec + dur)
        seg_timeline_info.append(f"- الجزء {idx:02d} ({aseg} / Visuals_part{idx:02d}.mp4): يبدأ من [{start_str}] وينتهي عند [{end_str}]")
        accum_sec += dur
    timeline_map_str = "\n".join(seg_timeline_info)

    system_prompt = (
        "أنت خبير تقييم أداء مقدمي البرامج التلفزيونية وفق السلم المعتمد (90 نقطة).\n"
        "قواعد إجبارية صارمة جداً:\n"
        "1. المنع المطلق للتخمين (Zero Hallucination): اعتمد حصرياً وبنسبة 100% على محتوى جميع ملفات الأجزاء المرفقة.\n"
        f"2. خريطة التوقيت المتصل للأجزاء (المدة الإجمالية {exact_duration}):\n{timeline_map_str}\n"
        "3. قاعدة التوقيت المطلق المتصل: اجمع توقيت اللقطة داخل الجزء مع وقت بداية ذلك الجزء الموضح في الخريطة أعلاه، لتكون كافة التوقيتات في التقرير متصلة من بداية الحلقة (00:00:00) حتى نهايتها (" + exact_duration + "). ممنوع نهائياً كتابة (الجزء 1 أو الجزء 2).\n"
        "4. التوثيق الحرفي لآخر كلام في الحلقة (lastSpokenWords): يجب عليك الاستماع إلى آخر دقيقة في آخر ملف صوتي مرفق، وتدوين النص الحرفي (Verbatim) لآخر جملتين أو ثلاث جمل قالها المقدم أو الضيف قبل انتهاء الحلقة مباشرة مع توقيتها المتصل.\n"
        "5. أرجع كائن JSON حصراً بالحقول التالية:\n"
        "   - isFullDurationAnalyzed: (boolean)\n"
        "   - analyzedAllSegments: (boolean) يجب أن يكون true حصراً إذا تم تحليل كافة الأجزاء حتى الثانية الأخيرة.\n"
        "   - lastSpokenWords: (string) النص الحرفي لآخر كلام قيل في ختام الحلقة مع التوقيت المتصل.\n"
        "   - mainDetailedHtml: (string) التقرير الرئيسي المفصل بالتوقيتات المتصلة.\n"
        "   - summarizedHtml: (string) التقرير المختصر."
    )

    errors = []
    for key_idx, api_key in enumerate(API_KEYS, start=1):
        log_step(f"🔑 فحص المفتاح Key #{key_idx}...")
        try:
            prompt_parts = []
            for seg in segments:
                log_step(f"☁️ رفع الجزء ({seg}) إلى Gemini باستخدام Key #{key_idx}...")
                uri, mime = upload_to_gemini_file_api(seg, api_key)
                prompt_parts.append({"text": f"الملف المرفق التالي هو: {seg}"})
                prompt_parts.append({"fileData": {"mimeType": mime, "fileUri": uri}})
        except Exception as up_err:
            errors.append(f"Key #{key_idx} Upload Error: {up_err}")
            continue

        for model_name, budget, label, is_highest in models:
            log_step(f"🧠 بدء التحليل العميق لجميع الأجزاء عبر النموذج: {label} (Key #{key_idx})...")
            gen_cfg = {"temperature": 0.0, "responseMimeType": "application/json"}
            if budget > 0:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}
            
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": prompt_parts + [{"text": f"قم بتحليل وتقييم حلقة ({CLEAN_NAME}) بالكامل عبر تغطية جميع الأجزاء المرفقة بدقة تامة وصولاً للثواني الأخيرة، واستخرج آخر كلام نُطق في ختام الحلقة حرفياً في حقل lastSpokenWords."}]}],
                "generationConfig": gen_cfg
            }
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            
            try:
                res = session.post(url, json=payload, timeout=600)
                if res.status_code == 200:
                    data = res.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(text)
                    
                    if not parsed.get("isFullDurationAnalyzed") or not parsed.get("analyzedAllSegments", True):
                        raise RuntimeError("أقر النموذج بعدم اكتمال تحليل كامل المقاطع والأجزاء.")
                    
                    if not parsed.get("lastSpokenWords") or len(parsed.get("lastSpokenWords", "").strip()) < 10:
                        raise RuntimeError("لم يتم استخراج النص الحرفي لآخر كلام في نهاية الحلقة.")

                    banner = "" if is_highest else f'<div class="thinking-note">مستوى التفكير: {label} (Key #{key_idx})</div>'
                    parsed["thinkingBanner"] = banner
                    return parsed
                else:
                    log_step(f"⚠️ خطأ خادم ({res.status_code}) على {label} - انتقال فوري للنموذج التالي...")
                    errors.append(f"Key #{key_idx} [{label}] HTTP {res.status_code}: {res.text}")
                    continue
            except Exception as req_err:
                log_step(f"⚠️ خطأ اتصال/مهلة على {label}: {req_err} - انتقال فوري للنموذج التالي...")
                errors.append(f"Key #{key_idx} [{label}] Error: {req_err}")
                continue

    raise RuntimeError("استنفدت جميع المفاتيح والنماذج أو تم رفض الطلبات بسبب ضغط الخوادم:\n" + "\n".join(errors))

def main():
    try:
        send_telegram(f"🚀 <b>بدء معالجة حلقة:</b> <code>{CLEAN_NAME}</code>")
        log_step(f"انطلاق معالجة الحلقة: {CLEAN_NAME}")
        
        drive_files = list_drive_media_files()
        if not drive_files:
            raise RuntimeError("المجلد لا يحتوي على أي ملفات فيديو أو صوت.")

        segments, exact_duration = prepare_media_segments(drive_files)
        total_segs_count = len(glob.glob("Audio_part*.mp3")) + len(glob.glob("Visuals_part*.mp4"))
        
        result = evaluate_with_gemini(segments, exact_duration, total_segs_count)

        log_step(f"✅ تم بنجاح تحليل وتغطية كامل مقاطع الحلقة ({total_segs_count} جزء) حتى النهاية بمدة إجمالية ({exact_duration}) دون أي نقص!")
        
        main_html = wrap_html(f"تقييم تفصيلي - {CLEAN_NAME}", result["thinkingBanner"] + result["mainDetailedHtml"])
        sum_html = wrap_html(f"تقرير مختصر - {CLEAN_NAME}", result["thinkingBanner"] + result["summarizedHtml"])

        upload_report_to_drive(f"01_Main_Detailed_Report_{CLEAN_NAME}.html", main_html)
        upload_report_to_drive(f"02_Summarized_Report_{CLEAN_NAME}.html", sum_html)

        last_spoken = result.get("lastSpokenWords", "غير متوفر").strip()

        elapsed = int(time.time() - START_TIME)
        elapsed_str = f"{elapsed // 60} دقيقة و {elapsed % 60} ثانية"
        
        summary_desc = (
            f"• عدد المقاطع المحللة: {total_segs_count} جزء\n"
            f"• المدة الإجمالية للحلقة: {exact_duration}\n"
            f"• الوقت المستغرق: {elapsed_str}\n"
            f"• آخر كلام نُطق في ختام الحلقة (Verbatim):\n«{last_spoken}»"
        )
        update_drive_folder(f"PROCESSED_{CLEAN_NAME}", summary_desc)

        folder_url = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
        send_telegram(
            f"✅ <b>تمت المعالجة والتقييم بنجاح تام وتم التأكد من الوصول للجزء الأخير!</b>\n"
            f"📌 الحلقة: <code>{html.escape(CLEAN_NAME)}</code>\n"
            f"📊 تم تحليل وتغطية كافة المقاطع ({total_segs_count} جزء) حتى النهاية.\n"
            f"⏱️ المدة الإجمالية: <code>{exact_duration}</code> | الوقت المستغرق: <code>{elapsed_str}</code>\n\n"
            f"🎙️ <b>آخر كلام نُطق في ختام الحلقة (Verbatim):</b>\n"
            f"«<i>{html.escape(last_spoken)}</i>»\n\n"
            f"📁 <a href='{folder_url}'>رابط المجلد على الدرايف</a>"
        )
    except Exception as e:
        log_step(f"❌ خطأ وفشل المهمة: {e}")
        error_msg = str(e)
        update_drive_folder(f"FAILED_{CLEAN_NAME}", f"السبب:\n{error_msg[:1000]}")
        
        # حماية النص من كسر تنسيق تيليغرام
        safe_error = html.escape(error_msg[-800:])
        send_telegram(
            f"❌ <b>فشل معالجة الحلقة:</b> <code>{html.escape(CLEAN_NAME)}</code>\n"
            f"⚠️ التنبيه: تم إيقاف المعالجة بسبب خطأ تقني أو استنفاد الحصة.\n"
            f"📝 <b>تفاصيل الخطأ الدقيقة:</b>\n"
            f"<pre>{safe_error}</pre>"
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
