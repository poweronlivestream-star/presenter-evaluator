import os
import sys
import time
import json
import glob
import subprocess
import re
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import html

FOLDER_ID = os.environ["FOLDER_ID"]
CLEAN_NAME = os.environ["CLEAN_NAME"].strip()
LOG_FILE_ID = os.environ["LOG_FILE_ID"]
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")
DRIVE_TOKEN = os.environ["DRIVE_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

API_KEYS = [
    k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()
]

# تحديد نوع المنظومة تلقائياً من لاحقة اسم المجلد (_LOW أو _PAID)
UPPER_NAME = CLEAN_NAME.upper()
IS_LOW_MODE = UPPER_NAME.endswith("_LOW")
FORCE_PAID = UPPER_NAME.endswith("_PAID")

if FORCE_PAID:
    MODE_TITLE = "المنظومة المدفوعة المباشرة (_PAID - الدقة القصوى)"
elif IS_LOW_MODE:
    MODE_TITLE = "المنظومة الاقتصادية (_LOW) (صورة كل 5 ثوان)"
else:
    MODE_TITLE = "المنظومة الاحترافية (الدقة القصوى - صورة كل ثانية) [تبدأ بالمجاني ثم المدفوع عند الفشل]"

START_TIME = time.time()
LOG_LINES = []

# إعدادات سقف الإنفاق الشهري (5.00 دولار كحد أقصى)
MAX_MONTHLY_BUDGET = 5.00
SPEND_TRACKER_FILENAME = "_gemini_monthly_spend_tracker.json"

session = requests.Session()
retries = Retry(total=5, backoff_factor=3, status_forcelist=[500, 502, 504], raise_on_status=False)
session.mount('https://', HTTPAdapter(max_retries=retries))

def send_telegram(message, use_html=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4000],
            "disable_web_page_preview": True
        }
        if use_html:
            payload["parse_mode"] = "HTML"
            
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code != 200 and use_html:
            clean_text = re.sub(r'<[^>]+>', '', message)[:4000]
            payload["text"] = clean_text
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"خطأ في إرسال تليغرام: {e}", flush=True)

def log_step(msg):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)
    
    send_telegram(f"🔔 تحديث معالجة ({CLEAN_NAME}):\n⚙️ {MODE_TITLE}\n{msg[:300]}", use_html=False)
    
    try:
        headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "text/plain; charset=utf-8"}
        session.patch(
            f"https://www.googleapis.com/upload/drive/v3/files/{LOG_FILE_ID}?uploadType=media",
            headers=headers,
            data="\n".join(LOG_LINES).encode("utf-8"),
            timeout=20
        )
    except Exception:
        pass

# ==============================================================================
# إدارة وحفظ ميزانية الـ 5$ الشهرية بشكل دائم على Google Drive
# ==============================================================================
def _find_spend_tracker_file_id():
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    q = f"name = '{SPEND_TRACKER_FILENAME}' and trashed = false"
    res = session.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={"q": q, "fields": "files(id, name)", "pageSize": 1},
        timeout=20
    )
    if res.status_code == 200:
        files = res.json().get("files", [])
        if files:
            return files[0]["id"]
    return None

def get_monthly_spend():
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        file_id = _find_spend_tracker_file_id()
        if not file_id:
            return 0.0
        headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
        res = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers=headers,
            timeout=20
        )
        if res.status_code == 200:
            data = res.json()
            if data.get("month") == current_month:
                return float(data.get("spent", 0.0))
    except Exception as e:
        print(f"تحذير أثناء قراءة سجل الإنفاق الشهري: {e}", flush=True)
    return 0.0

def add_monthly_spend(amount):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    current_spent = get_monthly_spend()
    new_spent = round(current_spent + float(amount), 6)
    payload_bytes = json.dumps({
        "month": current_month,
        "spent": new_spent,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).encode("utf-8")

    try:
        file_id = _find_spend_tracker_file_id()
        if file_id:
            headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json; charset=utf-8"}
            session.patch(
                f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media",
                headers=headers,
                data=payload_bytes,
                timeout=20
            )
        else:
            metadata = {"name": SPEND_TRACKER_FILENAME, "mimeType": "application/json"}
            boundary = "===boundary_spend_tracker==="
            body = (
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{payload_bytes.decode('utf-8')}\r\n"
                f"--{boundary}--"
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"multipart/related; boundary={boundary}"}
            session.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                headers=headers,
                data=body,
                timeout=30
            )
    except Exception as e:
        print(f"تحذير أثناء تحديث سجل الإنفاق الشهري: {e}", flush=True)
    return new_spent

def calculate_call_cost(model_name, usage_metadata):
    in_tokens = int(usage_metadata.get("promptTokenCount", 0) or 0)
    out_tokens = int(usage_metadata.get("candidatesTokenCount", 0) or 0)
    think_tokens = int(usage_metadata.get("thoughtsTokenCount", 0) or 0)
    total_out = out_tokens + think_tokens

    if "pro" in model_name.lower():
        in_rate = (1.25 / 1_000_000) if in_tokens <= 200_000 else (2.50 / 1_000_000)
        out_rate = (10.00 / 1_000_000) if in_tokens <= 200_000 else (15.00 / 1_000_000)
    else:
        in_rate = 0.50 / 1_000_000
        out_rate = 2.50 / 1_000_000

    cost = (in_tokens * in_rate) + (total_out * out_rate)
    return max(cost, 0.0001)

def get_prioritized_keys():
    """
    ترتيب المفاتيح:
    - المفتاح الأخير في GEMINI_API_KEYS يعتبر هو المفتاح المدفوع (PAID).
    - جميع المفاتيح التي قبله تعتبر مفاتيح مجانية (FREE #1, FREE #2, ...).
    - إذا كان اسم المجلد ينتهي بـ _paid، يتم الذهاب مباشرة للمفتاح المدفوع فقط.
    """
    if not API_KEYS:
        raise RuntimeError("لا توجد مفاتيح API معرفة في GEMINI_API_KEYS.")

    if len(API_KEYS) == 1:
        only_type = "PAID" if FORCE_PAID else "Key #1"
        return [(1, only_type, API_KEYS[0], FORCE_PAID)]

    paid_key = API_KEYS[-1]
    paid_idx = len(API_KEYS)
    free_keys = API_KEYS[:-1]

    if FORCE_PAID:
        log_step(f"💳 اسم المجلد ينتهي بـ _paid: الانتقال المباشر إلى المفتاح المدفوع (Key #{paid_idx})...")
        return [(paid_idx, f"PAID (Key #{paid_idx})", paid_key, True)]

    ordered = []
    for idx, fk in enumerate(free_keys, start=1):
        ordered.append((idx, f"FREE (Key #{idx})", fk, False))
    ordered.append((paid_idx, f"PAID (Key #{paid_idx})", paid_key, True))
    return ordered

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
                if chunk:
                    f.write(chunk)

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
        log_step(f"📥 سحب ملف الفيديو الخام ({raw_file['name']} - {size_mb:.1f} MB)...")
        local_raw = "raw_episode.mp4"
        download_drive_file(raw_file["id"], local_raw)
        total_sec = get_duration_seconds(local_raw)
        exact_duration = format_hhmmss(total_sec)
        log_step(f"⏱️ المدة الفعلية للحلقة: {exact_duration}")
        
        if IS_LOW_MODE:
            log_step("✂️ تطبيق إعدادات (_LOW): (صورة كل 5 ثوان - دقة متدنية) + استخراج الصوت...")
            subprocess.run(["ffmpeg", "-y", "-i", local_raw, "-vn", "-c:a", "libmp3lame", "-ac", "1", "-ar", "24000", "-b:a", "48k", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Audio_part%02d.mp3"], check=True)
            subprocess.run(["ffmpeg", "-y", "-i", local_raw, "-an", "-c:v", "libx264", "-preset", "faster", "-crf", "32", "-g", "5", "-maxrate", "100k", "-bufsize", "200k", "-vf", "scale=-2:480,fps=0.2", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Visuals_part%02d.mp4"], check=True)
        else:
            log_step("✂️ جاري تشغيل FFmpeg لاستخراج وتقسيم الصوت (MP3) كل 25 دقيقة...")
            subprocess.run(["ffmpeg", "-y", "-i", local_raw, "-vn", "-c:a", "libmp3lame", "-ac", "1", "-ar", "24000", "-b:a", "64k", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Audio_part%02d.mp3"], check=True)
            log_step("🎞️ جاري تشغيل FFmpeg لاستخراج وتقسيم الصورة (فيديو خفيف 720p - 1fps) كل 25 دقيقة...")
            subprocess.run(["ffmpeg", "-y", "-i", local_raw, "-an", "-c:v", "libx264", "-preset", "faster", "-crf", "30", "-maxrate", "150k", "-bufsize", "300k", "-vf", "scale=-2:720,fps=1", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Visuals_part%02d.mp4"], check=True)
        
        segments = sorted(glob.glob("Audio_part*.mp3")) + sorted(glob.glob("Visuals_part*.mp4"))
        return segments, exact_duration
    else:
        local_files = []
        total_audio_sec = 0.0
        for f in drive_files:
            log_step(f"📥 سحب الملف المقسم ({f['name']})...")
            download_drive_file(f["id"], f["name"])
            local_files.append(f["name"])
            if f["mimeType"].startswith("audio/"):
                total_audio_sec += get_duration_seconds(f["name"])
        exact_duration = format_hhmmss(total_audio_sec) if total_audio_sec > 0 else "غير محدد"
        return local_files, exact_duration

def upload_to_gemini_file_api(filepath, api_key):
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

# ==============================================================================
# المسار الأول: النسخة الاحترافية (ترفع وتحلل دفعة واحدة مع التبديل الذكي للمفاتيح)
# ==============================================================================
def evaluate_standard_pro_mode(segments, exact_duration, total_segments_count):
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
        seg_timeline_info.append(f"- الجزء {idx:02d} ({aseg}): [{start_str}] -> [{end_str}]")
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

    prioritized_keys = get_prioritized_keys()
    errors = []

    for key_idx, key_label, api_key, is_paid_key in prioritized_keys:
        log_step(f"🔑 فحص المفتاح {key_label}...")

        # فحص سقف الإنفاق الشهري (5$) قبل استخدام المفتاح المدفوع
        if is_paid_key:
            current_monthly_spend = get_monthly_spend()
            if current_monthly_spend >= MAX_MONTHLY_BUDGET:
                ceil_msg = (
                    f"تم إيقاف العملية: وصل الإنفاق الشهري إلى الحد الأقصى المسموح به "
                    f"(${current_monthly_spend:.4f} / ${MAX_MONTHLY_BUDGET:.2f})."
                )
                log_step(f"🛑 {ceil_msg}")
                errors.append(f"• {key_label}: {ceil_msg}")
                raise RuntimeError("\n".join(errors))
            log_step(f"💰 فحص سقف الميزانية للمفتاح المدفوع: المستهلك هذا الشهر ${current_monthly_spend:.4f} من أصل ${MAX_MONTHLY_BUDGET:.2f}")

        try:
            prompt_parts = []
            for seg in segments:
                log_step(f"☁️ رفع الجزء ({seg}) إلى Gemini باستخدام {key_label}...")
                uri, mime = upload_to_gemini_file_api(seg, api_key)
                prompt_parts.append({"text": f"الملف المرفق التالي هو: {seg}"})
                prompt_parts.append({"fileData": {"mimeType": mime, "fileUri": uri}})
        except Exception as up_err:
            log_step(f"⚠️ فشل رفع الملفات عبر {key_label} - الانتقال للمفتاح التالي...")
            errors.append(f"• {key_label}: فشل رفع الملفات ({str(up_err)[:80]})")
            continue

        for model_name, budget, label, is_highest in models:
            log_step(f"🧠 بدء التحليل العميق لجميع الأجزاء عبر النموذج: {label} ({key_label})...")
            gen_cfg = {"temperature": 0.0, "responseMimeType": "application/json"}
            if budget > 0:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}
            
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": prompt_parts + [{"text": f"قم بتحليل وتقييم حلقة ({CLEAN_NAME}) بالكامل."}]}],
                "generationConfig": gen_cfg
            }
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            
            try:
                res = session.post(url, json=payload, timeout=600)
                if res.status_code == 200:
                    data = res.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(text)

                    process_cost = 0.0
                    if is_paid_key:
                        usage = data.get("usageMetadata", {})
                        process_cost = calculate_call_cost(model_name, usage)
                        total_monthly = add_monthly_spend(process_cost)
                        log_step(f"💵 تكلفة هذه العملية: ${process_cost:.4f} | إجمالي إنفاق الشهر: ${total_monthly:.4f} / ${MAX_MONTHLY_BUDGET:.2f}")
                    else:
                        total_monthly = get_monthly_spend()

                    banner = "" if is_highest else f'<div class="thinking-note">مستوى التفكير: {label} ({key_label})</div>'
                    parsed["thinkingBanner"] = banner
                    parsed["costInfo"] = {
                        "keyLabel": key_label,
                        "isPaid": is_paid_key,
                        "processCost": process_cost,
                        "totalMonthly": total_monthly
                    }
                    return parsed
                elif res.status_code == 429:
                    err_msg = res.json().get("error", {}).get("message", "")
                    match = re.search(r'Please retry in ([a-zA-Z0-9.]+)', err_msg)
                    wait_time = match.group(1).split('.')[0] if match else "مؤقت"
                    clean_err = f"المفتاح مستنفد (يعمل بعد: {wait_time})"
                    log_step(f"⚠️ {clean_err} على {key_label} - انتقال للنموذج/المفتاح التالي...")
                    errors.append(f"• {key_label} [{label}]: {clean_err}")
                    continue
                elif res.status_code == 503:
                    log_step(f"⚠️ ضغط سيرفرات جوجل (503) على {label} ({key_label}) - انتقال للنموذج/المفتاح التالي...")
                    errors.append(f"• {key_label} [{label}]: ضغط سيرفرات جوجل (503)")
                    continue
                else:
                    log_step(f"⚠️ استجابة HTTP {res.status_code} على {label} ({key_label}) - انتقال للنموذج/المفتاح التالي...")
                    errors.append(f"• {key_label} [{label}]: HTTP {res.status_code}")
                    continue
            except Exception as req_err:
                log_step(f"⚠️ خطأ اتصال على {label} ({key_label}) - انتقال للنموذج التالي...")
                errors.append(f"• {key_label} [{label}]: خطأ شبكة ({str(req_err)[:50]})")
                continue

    raise RuntimeError("\n".join(errors))

# ==============================================================================
# المسار الثاني: نسخة (_LOW) - تحليل كل مقطع على حدة مع التبديل الذكي للمفاتيح
# ==============================================================================
def call_gemini_low_mode_step(system_prompt, user_parts_builder, models_list, step_title, expect_json=True):
    prioritized_keys = get_prioritized_keys()
    errors = []

    for key_idx, key_label, api_key, is_paid_key in prioritized_keys:
        if is_paid_key:
            current_monthly_spend = get_monthly_spend()
            if current_monthly_spend >= MAX_MONTHLY_BUDGET:
                ceil_msg = (
                    f"تم إيقاف العملية: وصل الإنفاق الشهري إلى الحد الأقصى المسموح به "
                    f"(${current_monthly_spend:.4f} / ${MAX_MONTHLY_BUDGET:.2f})."
                )
                log_step(f"🛑 {ceil_msg}")
                errors.append(f"• {key_label}: {ceil_msg}")
                raise RuntimeError("\n".join(errors))

        try:
            prompt_parts = user_parts_builder(api_key, key_label)
        except Exception as up_err:
            errors.append(f"• {key_label} ({step_title}): فشل رفع الملفات ({str(up_err)[:50]})")
            continue

        for model_name, budget, label, is_highest in models_list:
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                log_step(f"🧠 [{step_title}] عبر {label} ({key_label} - محاولة {attempt}/{max_attempts})...")
                gen_cfg = {"temperature": 0.0}
                if expect_json:
                    gen_cfg["responseMimeType"] = "application/json"
                if budget > 0:
                    gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}

                payload = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": prompt_parts}],
                    "generationConfig": gen_cfg
                }
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

                try:
                    res = session.post(url, json=payload, timeout=600)
                    if res.status_code == 200:
                        data = res.json()
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        step_cost = 0.0
                        if is_paid_key:
                            usage = data.get("usageMetadata", {})
                            step_cost = calculate_call_cost(model_name, usage)
                            add_monthly_spend(step_cost)
                        return text, label, key_label, is_paid_key, step_cost

                    elif res.status_code == 429:
                        err_msg = res.json().get("error", {}).get("message", "")
                        match = re.search(r'Please retry in ([a-zA-Z0-9.]+)', err_msg)
                        wait_time = match.group(1).split('.')[0] if match else "مؤقت"
                        clean_err = f"الحصة مستنفدة 429 (يعمل بعد: {wait_time})"

                        if "h" not in wait_time and attempt < max_attempts:
                            log_step(f"⏳ [{step_title}] امتلاء عداد الدقيقة ({wait_time}). تهدئة 65 ثانية ثم الإعادة...")
                            time.sleep(65)
                            continue

                        log_step(f"⚠️ [{step_title}] {clean_err} على {key_label} - انتقال للنموذج/المفتاح التالي...")
                        errors.append(f"• {key_label} [{label}]: {clean_err}")
                        break

                    elif res.status_code == 503:
                        if attempt < max_attempts:
                            wait_sec = 45 * attempt
                            log_step(f"⏳ [{step_title}] ضغط سيرفرات جوجل (503) على {label}. انتظار {wait_sec} ثانية ثم الإعادة...")
                            time.sleep(wait_sec)
                            continue
                        log_step(f"⚠️ [{step_title}] استمرار ضغط السيرفرات (503) على {label} - انتقال للتالي...")
                        errors.append(f"• {key_label} [{label}]: ضغط سيرفرات جوجل (503)")
                        break
                    else:
                        errors.append(f"• {key_label} [{label}]: HTTP {res.status_code}")
                        break
                except Exception as req_err:
                    log_step(f"⚠️ [{step_title}] خطأ اتصال مؤقت: {str(req_err)[:50]}...")
                    errors.append(f"• {key_label} [{label}]: {str(req_err)[:50]}")
                    if attempt < max_attempts:
                        time.sleep(30)
                        continue
                    break

    raise RuntimeError("\n".join(errors))

def evaluate_low_free_mode(segments, exact_duration, total_segments_count):
    models = [
        ("gemini-3.8-flash", 0,    "gemini-3.8-flash (Standard Fast)", True),
        ("gemini-3.8-flash", 8192, "gemini-3.8-flash (Thinking 8192)", False)
    ]

    audio_segs = sorted(glob.glob("Audio_part*.mp3"))
    visual_segs = sorted(glob.glob("Visuals_part*.mp4"))
    num_parts = len(audio_segs)

    part_configs = []
    accum_sec = 0.0
    for idx, aseg in enumerate(audio_segs):
        dur = get_duration_seconds(aseg)
        start_str = format_hhmmss(accum_sec)
        end_str = format_hhmmss(accum_sec + dur)
        vseg = visual_segs[idx] if idx < len(visual_segs) else None
        part_configs.append({
            "idx": idx,
            "audio": aseg,
            "visual": vseg,
            "start_str": start_str,
            "end_str": end_str
        })
        accum_sec += dur

    part_summaries = []
    used_label = ""
    used_key_label = ""
    any_paid_used = False
    total_process_cost = 0.0

    for p in part_configs:
        idx = p["idx"]
        is_last_part = (idx == num_parts - 1)
        step_title = f"تحليل المقطع {idx + 1}/{num_parts} ({p['start_str']} -> {p['end_str']})"

        part_sys_prompt = (
            "أنت خبير تقييم أداء مقدمي البرامج التلفزيونية وفق السلم المعتمد (90 نقطة).\n"
            f"أنت الآن تحلل المقطع رقم ({idx + 1} من {num_parts}) والذي يمتد في الحلقة الأصلية من التوقيت [{p['start_str']}] إلى [{p['end_str']}].\n"
            "قواعد صارمة:\n"
            f"1. قاعدة التوقيت المتصل: أضف دائماً وقت بداية هذا المقطع ({p['start_str']}) إلى أي توقيت ترصده داخل الملف، لتكون جميع التوقيتات المدونة بين [{p['start_str']}] و [{p['end_str']}].\n"
            "2. استخرج تقريراً تحليلياً تفصيلياً وشاملاً لهذا المقطع يغطي كافة معايير سلم الـ 90 نقطة مع ذكر الشواهد والاقتباسات الحرفية بتوقيتاتها المتصلة.\n"
            + ("3. هام جداً (المقطع الأخير): استمع بدقة إلى الدقيقة الأخيرة من هذا المقطع واكتب في نهاية تحليلك تحت عنوان [LAST_SPOKEN_WORDS] النص الحرفي (Verbatim) لآخر جملتين أو ثلاث جمل قالها المقدم أو الضيف قبل ختام الحلقة مع توقيتها المتصل." if is_last_part else "")
        )

        def build_part_upload(api_key, key_label, part_info=p):
            files_to_up = [part_info["audio"]]
            if part_info["visual"]:
                files_to_up.append(part_info["visual"])
            parts_list = []
            for fpath in files_to_up:
                log_step(f"☁️ رفع ({fpath}) للمقطع {part_info['idx'] + 1}/{num_parts} عبر {key_label}...")
                uri, mime = upload_to_gemini_file_api(fpath, api_key)
                parts_list.append({"text": f"الملف المرفق: {fpath}"})
                parts_list.append({"fileData": {"mimeType": mime, "fileUri": uri}})
            parts_list.append({
                "text": f"حلل هذا المقطع الممتد من [{part_info['start_str']}] إلى [{part_info['end_str']}] بالتفصيل وبتوقيتات متصلة."
            })
            return parts_list

        part_text, used_label, used_key_label, is_paid, step_cost = call_gemini_low_mode_step(
            system_prompt=part_sys_prompt,
            user_parts_builder=build_part_upload,
            models_list=models,
            step_title=step_title,
            expect_json=False
        )
        if is_paid:
            any_paid_used = True
            total_process_cost += step_cost
        part_summaries.append(f"=== تحليل المقطع {idx + 1} (من {p['start_str']} إلى {p['end_str']}) ===\n{part_text}")

        if not is_last_part:
            log_step("⏳ تهدئة 35 ثانية قبل المقطع التالي لإراحة عداد التوكنز...")
            time.sleep(35)

    log_step("📊 دمج تحليلات جميع المقاطع وإصدار التقرير النهائي الموحد (90 نقطة)...")
    combined_notes = "\n\n".join(part_summaries)

    final_sys_prompt = (
        "أنت خبير تقييم أداء مقدمي البرامج التلفزيونية وفق السلم المعتمد (90 نقطة).\n"
        f"أمامك التحليلات التفصيلية الكاملة لجميع مقاطع الحلقة ({CLEAN_NAME}) والبالغة مدتها الإجمالية ({exact_duration}).\n"
        "قواعد إجبارية صارمة:\n"
        "1. قم بدمج وتوحيد هذه التحليلات في تقييم نهائي واحد متكامل وشامل للحلقة كاملة من (00:00:00) حتى (" + exact_duration + ") وفق سلم الـ 90 نقطة.\n"
        "2. يمنع نهائياً كتابة عبارات مثل (في الجزء الأول أو المقطع الثاني)، بل تعامل مع الحلقة كوحدة متصلة بالتوقيتات المطلقة.\n"
        "3. استخرج النص الحرفي لآخر كلام قيل في ختام الحلقة مع توقيته المتصل وضعه في حقل lastSpokenWords.\n"
        "4. أرجع كائن JSON حصراً بالحقول التالية:\n"
        "   - isFullDurationAnalyzed: (boolean) ضعه true\n"
        "   - analyzedAllSegments: (boolean) ضعه true\n"
        "   - lastSpokenWords: (string) النص الحرفي لآخر كلام قيل في ختام الحلقة مع التوقيت المتصل.\n"
        "   - mainDetailedHtml: (string) التقرير الرئيسي المفصل والمنسق بـ HTML.\n"
        "   - summarizedHtml: (string) التقرير المختصر والمنسق بـ HTML."
    )

    def build_final_text_parts(api_key, key_label):
        return [{"text": f"فيما يلي التحليل المفصل لكافة مقاطع الحلقة من البداية للنهاية، قم بإصدار كائن JSON النهائي للتقييم:\n\n{combined_notes}"}]

    final_json_str, used_label, used_key_label, is_paid, step_cost = call_gemini_low_mode_step(
        system_prompt=final_sys_prompt,
        user_parts_builder=build_final_text_parts,
        models_list=models,
        step_title="إصدار التقرير النهائي الموحد",
        expect_json=True
    )
    if is_paid:
        any_paid_used = True
        total_process_cost += step_cost

    parsed = json.loads(final_json_str)
    parsed["thinkingBanner"] = (
        '<div class="low-mode-banner">'
        '⚠️ <b>تنبيه نوع المنظومة:</b> تم اختيار <b>المنظومة الاقتصادية (_LOW) (صورة كل 5 ثوان)</b>. '
        'للحصول على الدقة التحليلية القصوى (صورة كل ثانية)، يرجى تسمية المجلد بدون لاحقة <code>_LOW</code>.'
        '</div>'
        f'<div class="thinking-note">المنظومة: {MODE_TITLE} | النموذج: {used_label} ({used_key_label})</div>'
    )
    parsed["costInfo"] = {
        "keyLabel": used_key_label,
        "isPaid": any_paid_used,
        "processCost": total_process_cost,
        "totalMonthly": get_monthly_spend()
    }
    return parsed

def evaluate_with_gemini(segments, exact_duration, total_segments_count):
    if IS_LOW_MODE:
        return evaluate_low_free_mode(segments, exact_duration, total_segments_count)
    else:
        return evaluate_standard_pro_mode(segments, exact_duration, total_segments_count)

def wrap_html(title, body_content):
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: "Arial", sans-serif; font-size: 14pt; line-height: 1.6; direction: rtl; text-align: right; color: #222222; max-width: 900px; margin: 40px auto; padding: 20px; background-color: #fdfdfd; }}
  .low-mode-banner {{ background-color: #FFF3CD; color: #856404; border: 2px solid #FFEEBA; border-right: 6px solid #FFC107; padding: 12px 16px; margin-bottom: 18px; border-radius: 4px; font-size: 12pt; }}
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

def main():
    try:
        send_telegram(f"🚀 بدء معالجة حلقة: {CLEAN_NAME}\n⚙️ {MODE_TITLE}", use_html=False)
        log_step(f"انطلاق معالجة الحلقة: {CLEAN_NAME} | {MODE_TITLE}")
        
        drive_files = list_drive_media_files()
        if not drive_files:
            raise RuntimeError("المجلد لا يحتوي على أي ملفات فيديو أو صوت.")

        segments, exact_duration = prepare_media_segments(drive_files)
        total_segs_count = len(glob.glob("Audio_part*.mp3")) + len(glob.glob("Visuals_part*.mp4"))
        
        result = evaluate_with_gemini(segments, exact_duration, total_segs_count)

        log_step(f"✅ تم بنجاح تحليل كامل مقاطع الحلقة ({total_segs_count} جزء) بمدة ({exact_duration})!")
        
        main_html = wrap_html(f"تقييم تفصيلي - {CLEAN_NAME}", result.get("thinkingBanner", "") + result["mainDetailedHtml"])
        sum_html = wrap_html(f"تقرير مختصر - {CLEAN_NAME}", result.get("thinkingBanner", "") + result["summarizedHtml"])

        upload_report_to_drive(f"01_Main_Detailed_Report_{CLEAN_NAME}.html", main_html)
        upload_report_to_drive(f"02_Summarized_Report_{CLEAN_NAME}.html", sum_html)

        last_spoken = result.get("lastSpokenWords", "غير متوفر").strip()
        elapsed = int(time.time() - START_TIME)
        elapsed_str = f"{elapsed // 60} دقيقة و {elapsed % 60} ثانية"

        cost_info = result.get("costInfo", {
            "keyLabel": "FREE",
            "isPaid": False,
            "processCost": 0.0,
            "totalMonthly": get_monthly_spend()
        })

        # هذا الوصف يقرأه Google Apps Script ويرسله في الإيميل تلقائياً
        summary_desc = (
            f"• المنظومة المختارة: {MODE_TITLE}\n"
            f"• الحساب المستخدم: {cost_info['keyLabel']}\n"
            f"• تكلفة هذه العملية: ${cost_info['processCost']:.4f}\n"
            f"• إجمالي الإنفاق هذا الشهر: ${cost_info['totalMonthly']:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"• عدد المقاطع المحللة: {total_segs_count} جزء\n"
            f"• المدة الإجمالية للحلقة: {exact_duration}\n"
            f"• الوقت المستغرق: {elapsed_str}\n"
            f"• آخر كلام نُطق في ختام الحلقة:\n«{last_spoken}»"
        )
        update_drive_folder(f"PROCESSED_{CLEAN_NAME}", summary_desc)

        folder_url = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
        send_telegram(
            f"✅ تمت المعالجة والتقييم بنجاح!\n"
            f"📌 الحلقة: {CLEAN_NAME}\n"
            f"⚙️ المنظومة: {MODE_TITLE}\n"
            f"🔑 الحساب المستخدم: {cost_info['keyLabel']}\n"
            f"💵 تكلفة العملية الحالية: ${cost_info['processCost']:.4f}\n"
            f"📊 إجمالي الإنفاق هذا الشهر: ${cost_info['totalMonthly']:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"⏱️ المدة: {exact_duration} | الاستغراق: {elapsed_str}\n\n"
            f"🎙️ آخر كلام في الحلقة:\n«{last_spoken}»\n\n"
            f"📁 رابط المجلد:\n{folder_url}",
            use_html=False
        )
    except Exception as e:
        log_step("❌ توقفت المعالجة بسبب خطأ.")
        error_msg = str(e)
        current_monthly = get_monthly_spend()
        fail_desc = (
            f"• المنظومة المختارة: {MODE_TITLE}\n"
            f"• إجمالي الإنفاق هذا الشهر: ${current_monthly:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"• تفاصيل السبب:\n{error_msg[:1500]}"
        )
        update_drive_folder(f"FAILED_{CLEAN_NAME}", fail_desc)
        
        raw_fail_msg = (
            f"❌ فشل معالجة الحلقة: {CLEAN_NAME}\n"
            f"⚙️ المنظومة: {MODE_TITLE}\n"
            f"📊 إجمالي الإنفاق هذا الشهر: ${current_monthly:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"⚠️ تفاصيل الخطأ:\n{error_msg[:3000]}"
        )
        send_telegram(raw_fail_msg, use_html=False)
        sys.exit(1)

if __name__ == "__main__":
    main()
