#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
منظومة تقييم مقدمي البرامج التلفزيونية المحصنة (evaluate.py)
- مطابقة 100% في الشكل والمعايير والقوالب (90 نقطة - 6 معايير أساسية - 23 معياراً فرعياً - التقرير المفصل + التقرير المختصر).
- فحص مسبق فوري للحصة على المفاتيح المجانية خلال نصف ثانية لمنع إعادة الترفيع وضياع الوقت عند استنفاد الحصة.
- إدراج سياسة اللهجة اللبنانية المحكية ضمن التوجيهات (ليست عيباً إطلاقاً ولا يُخصم عليها).
- تنسيق HTML تنفيذي مخصص متوافق 100% مع Microsoft Word (ترويسة كحلية، صناديق ملونة، تيمزات واضحة).
"""

import os
import sys
import time
import json
import glob
import shutil
import smtplib
import subprocess
import re
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import html

FOLDER_ID = os.environ["FOLDER_ID"].strip()
CLEAN_NAME = os.environ.get("CLEAN_NAME", "Episode").strip()
LOG_FILE_ID = os.environ.get("LOG_FILE_ID", "").strip()
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "").strip()
DRIVE_TOKEN = os.environ["DRIVE_TOKEN"].strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

BACKUP_DIR = os.path.abspath("./backup_reports")
os.makedirs(BACKUP_DIR, exist_ok=True)

# جمع المفاتيح المجانية والمدفوعة
_free_env = (os.environ.get("GEMINI_FREE_API_KEYS", "") + "," + os.environ.get("GEMINI_API_KEYS", "")).strip(",")
_paid_env = (os.environ.get("GEMINI_PAID_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")).strip()
_legacy_list = [k.strip() for k in re.split(r"[,;\s]+", os.environ.get("GEMINI_API_KEYS", "")) if k.strip()]

if not _paid_env and len(_legacy_list) >= 2:
    _paid_env = _legacy_list[-1]
    _free_env = ",".join(_legacy_list[:-1])
elif not _paid_env and len(_legacy_list) == 1:
    _paid_env = _legacy_list[0]

FREE_API_KEYS = []
_seen_k = set()
for _k in re.split(r"[,;\s]+", _free_env):
    _k = _k.strip()
    if _k and _k not in _seen_k and _k != _paid_env:
        _seen_k.add(_k)
        FREE_API_KEYS.append(_k)

PAID_API_KEY = _paid_env if _paid_env else None
API_KEYS = FREE_API_KEYS + ([PAID_API_KEY] if PAID_API_KEY else [])

UPPER_NAME = CLEAN_NAME.upper()
FORCE_FREE = UPPER_NAME.endswith("_FREE") or "_FREE_" in UPPER_NAME
IS_LOW_MODE = UPPER_NAME.endswith("_LOW") or "_LOW_" in UPPER_NAME

START_TIME = time.time()
LOG_LINES = []
ORIGINAL_SOURCE_URL = None

MAX_MONTHLY_BUDGET = float(os.environ.get("MAX_MONTHLY_BUDGET", "5.00"))
DEFAULT_MAX_EPISODE_COST = float(os.environ.get("MAX_EPISODE_COST", "0.50"))
SPEND_TRACKER_FILENAME = "_gemini_monthly_spend_tracker.json"


def parse_episode_max_cost(folder_clean_name, default_cap=DEFAULT_MAX_EPISODE_COST):
    upper = folder_clean_name.upper()
    m_paid_custom = re.search(r'_PAID_(\d+(?:\.\d+)?)', upper)
    if m_paid_custom:
        val = float(m_paid_custom.group(1))
        if val > 10.0:
            val = val / 100.0
        return round(val, 4), f"محدد من اسم المجلد (_paid_{val:.2f}$)", False

    m_cents = re.search(r'_(?:MAX|CAP)?(\d+(?:\.\d+)?)C(?:ENTS)?(?=_|$)', upper)
    if m_cents:
        cents = float(m_cents.group(1))
        return round(cents / 100.0, 4), f"محدد من اسم المجلد ({cents:g} سنت = ${cents/100.0:.2f})", False

    m_max = re.search(r'_(?:MAX|CAP)(\d+(?:\.\d+)?)\$?(?=_|$)', upper)
    if m_max:
        val = float(m_max.group(1))
        if val > 5.0 and "$" not in m_max.group(0):
            return round(val / 100.0, 4), f"محدد من اسم المجلد ({val:g} سنت = ${val/100.0:.2f})", False
        return round(val, 4), f"محدد من اسم المجلد (${val:.2f})", False

    if upper.endswith("_PAID") or "_PAID_" in upper:
        return round(default_cap, 4), f"وضع أقل تكلفة ممكنة (_paid - سقف ${default_cap:.2f})", True

    return round(default_cap, 4), f"السقف الافتراضي للحلقة (${default_cap:.2f})", False


EPISODE_MAX_COST, EPISODE_COST_SOURCE, IS_PAID_MIN_COST = parse_episode_max_cost(CLEAN_NAME)
FORCE_PAID = ((UPPER_NAME.endswith("_PAID") or "_PAID_" in UPPER_NAME) and not FORCE_FREE)

if FORCE_FREE and IS_LOW_MODE:
    MODE_TITLE = "المنظومة المجانية الإجبارية الاقتصادية (_LOW_FREE)"
elif FORCE_FREE:
    MODE_TITLE = "المنظومة المجانية الإجبارية (_FREE)"
elif FORCE_PAID and IS_LOW_MODE:
    MODE_TITLE = f"المنظومة المدفوعة الاقتصادية (_LOW_PAID - {EPISODE_COST_SOURCE})"
elif IS_PAID_MIN_COST:
    MODE_TITLE = f"المنظومة الذكية بأقل تكلفة ممكنة (_PAID - تبدأ بالمجاني ثم الأوفر مالياً | {EPISODE_COST_SOURCE})"
elif FORCE_PAID:
    MODE_TITLE = f"المنظومة المدفوعة المتدرجة ({EPISODE_COST_SOURCE})"
elif IS_LOW_MODE:
    MODE_TITLE = f"المنظومة الاقتصادية (_LOW) [{EPISODE_COST_SOURCE}]"
else:
    MODE_TITLE = f"المنظومة الاحترافية المتدرجة [{EPISODE_COST_SOURCE}]"

SKILL_INSTRUCTIONS_AND_RUBRIC = """
أنت خبير متخصص في تقييم الأداء المهني والتلفزيوني لمقدمي البرامج السياسية والحوارية وفق السلم المعتمد (90 نقطة).
يجب عليك الالتزام الحرفي والصارم بنسبة 100% بجميع القواعد والمعايير والقوالب التالية دون أي اختصار أو اجتهاد خارجها:

================================================================================
أولاً: القواعد الإلزامية الصارمة (PROJECT INSTRUCTIONS)
================================================================================
1. قاعدة اللهجة اللبنانية المحكية (Lebanese Dialect Rule - جوهرية للغاية):
   - استخدام المقدم للهجة العامية اللبنانية الطبيعية أو تطعيم الحوار بها ليس أمراً سلبياً على الإطلاق، ويُمنع منعاً باتاً خصم أي علامة بسببه في أي معيار.
   - يُمنع منعاً باتاً إدراج "استخدام العامية أو اللهجة اللبنانية" ضمن نقاط الضعف أو "أولويات التطوير" أو المآخذ السلبية.
   - التطعيم المتوازن باللهجة اللبنانية المحكية أسلوب تواصلي إيجابي ومحبب يكسر الجمود والركود ("البيئة الناشفة" للفصحى الصرفة) ويقرب المقدم من المشاهد.
   - في معيار "اللغة والطلاقة (2 نقطة)"، تقتصر المحاسبة حصراً على: التلعثم، التردد المربك، مخارج الحروف، أو الأخطاء النحوية واللغوية الصريحة المرتکبة أثناء التحدث باللغة العربية الفصحى.

2. المنع المطلق للتخمين (Zero Hallucination Policy):
   - يُمنع منعاً باتاً توقع أو استنتاج أو تخمين أي معلومة غير موجودة صراحة في الفيديو أو الصوت المرفق.
   - إذا كانت أي معلومة أساسية غير مذكورة صراحة في المقطع، يُكتب مكانها "غير محدد في المقطع".

3. التحقق الإلزامي من مدة الحلقة وتحليلها بالكامل (Full Duration Verification):
   - يجب تحليل الحلقة بالكامل من الثانية الأولى وحتى آخر ثانية في المقطع الأخير دون أي انقطاع.
   - التوقيت يكتب بصيغة واضحة ومتصلة من بداية الحلقة: `من 00:08:24 إلى 00:08:28`.

4. مراعاة الخط التحريري وسياق القناة:
   - يُجرى التقييم مع الأخذ في الحسبان الهوية التحريرية للقناة (إعلام داعم ومتبنٍّ لخط المقاومة). ممنوع خصم علامات بسبب استخدام مصطلحات المقاومة.
   - حدود التوظيف التحريري: المونولوج الذي يتجاوز 3 دقائق متصلة دون أي مقاطعة وظيفية أو سؤال توجيهي من المقدم يُعد فشلاً تقنياً في إدارة الإيقاع (Broken TV Rhythm)، ويجب المحاسبة عليه بخصم النقاط.

================================================================================
ثانياً: سلم العلامات المعتمد حصرياً (90 نقطة)
================================================================================
1) إدارة الحلقة — 20 نقطة.
2) التعامل مع الضيف — 20 نقطة.
3) جودة الأسئلة — 20 نقطة.
4) المتابعة والتعقيب — 15 نقطة.
5) الحضور والأداء — 10 نقاط.
6) ضبط الوقت — 5 نقاط.

================================================================================
ثالثاً: القالب الإلزامي الحرفي لمخرجات الـ HTML (template.md)
================================================================================
1. حقل `mainDetailedHtml` يحتوي كود HTML الكامل لـ "التقرير الرئيسي المفصل" مع 6 جداول فرعية وجدول الأمثلة الموثقة (10 أمثلة على الأقل).
2. حقل `summarizedHtml` يحتوي كود HTML الكامل لـ "التقرير المختصر".
"""

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
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"خطأ في إرسال تليغرام: {e}", flush=True)


def send_telegram_document(file_path, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not os.path.exists(file_path):
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    for attempt in range(3):
        try:
            with open(file_path, "rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
                    files={"document": (os.path.basename(file_path), f, "text/html")},
                    timeout=60
                )
            if r.status_code == 200:
                print(f"✅ تم إرسال نسخة احتياطية من {os.path.basename(file_path)} إلى Telegram بنجاح!", flush=True)
                return True
        except Exception:
            time.sleep(3)
    return False


def send_direct_smtp_email(subject, html_body, attachment_paths=None):
    smtp_user = os.environ.get("GMAIL_USER", "").strip()
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipients_raw = os.environ.get("ALERT_EMAILS", os.environ.get("NOTIFY_EMAIL", smtp_user)).strip()
    if not smtp_user or not smtp_pass or not recipients_raw:
        return False

    recipients = [r.strip() for r in re.split(r"[,;]", recipients_raw) if r.strip()]
    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    for path in (attachment_paths or []):
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
            msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        print(f"✅ تم إرسال الإيميل المباشر عبر SMTP إلى: {recipients}", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ ملاحظة SMTP المباشر: {e}", flush=True)
        return False


def beautify_report_html(raw_html: str, run_cost_usd: float = 0.0) -> str:
    """
    تنسيق وتجميل تقرير التقييم (HTML) بألوان تنفيذية واضحة ومتوافقة مع Microsoft Word (RTL):
    - ترويسة كحلية ملكية (.header-banner)
    - صناديق زرقاء للخلاصة التنفيذية (.exec-box)
    - صندوق أخضر زمردي لنقاط القوة (.strengths-box)
    - صندوق كهرماني لأولويات التطوير (.priorities-box)
    - تلوين خلايا العلامات في الجداول
    """
    if not raw_html:
        return ""

    html_content = re.sub(r"^```html\s*", "", raw_html.strip(), flags=re.IGNORECASE)
    html_content = re.sub(r"^```\s*", "", html_content)
    html_content = re.sub(r"\s*```$", "", html_content)

    executive_css = """
    <style>
        @page { size: A4; margin: 1.8cm; }
        body {
            font-family: "Segoe UI", "Tahoma", "Arial", sans-serif;
            font-size: 12pt;
            line-height: 1.65;
            direction: rtl;
            text-align: right;
            color: #0F172A;
            max-width: 980px;
            margin: 24px auto;
            padding: 24px;
            background-color: #FFFFFF;
        }
        .header-banner {
            background-color: #1E3A8A;
            color: #FFFFFF;
            padding: 22px 24px;
            border-radius: 8px;
            border-bottom: 5px solid #F59E0B;
            margin-bottom: 22px;
            text-align: center;
        }
        .header-banner h1, h1 {
            text-align: center;
            font-size: 20pt;
            font-weight: bold;
            color: #FFFFFF;
            margin: 0 0 6px 0;
            page-break-after: avoid;
        }
        .subtitle {
            text-align: center;
            font-size: 13.5pt;
            font-weight: bold;
            color: #E2E8F0;
            margin: 0;
            page-break-after: avoid;
        }
        h2 {
            font-size: 14.5pt;
            font-weight: bold;
            color: #1E3A8A;
            background-color: #F1F5F9;
            border-right: 6px solid #1E3A8A;
            border-bottom: 1px solid #CBD5E1;
            padding: 8px 14px;
            margin-top: 24px;
            margin-bottom: 12px;
            border-radius: 4px;
            page-break-after: avoid;
        }
        p { margin-top: 6px; margin-bottom: 12px; text-align: justify; }
        .exec-box {
            background-color: #EFF6FF;
            border: 1px solid #BFDBFE;
            border-right: 6px solid #1D4ED8;
            padding: 14px 18px;
            border-radius: 6px;
            margin-bottom: 18px;
        }
        .strengths-box {
            background-color: #F0FDF4;
            border: 1px solid #BBF7D0;
            border-right: 6px solid #16A34A;
            padding: 14px 20px;
            border-radius: 6px;
            margin-bottom: 18px;
        }
        .strengths-box li { color: #064E3B; font-weight: 500; }
        .priorities-box {
            background-color: #FFFBEB;
            border: 1px solid #FDE68A;
            border-right: 6px solid #D97706;
            padding: 14px 20px;
            border-radius: 6px;
            margin-bottom: 18px;
        }
        .priorities-box li { color: #78350F; font-weight: 500; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 18px;
            page-break-inside: avoid;
        }
        table, th, td { border: 1px solid #CBD5E1; }
        th, td {
            padding: 9px 12px;
            text-align: right;
            vertical-align: top;
            page-break-inside: avoid;
        }
        tr { page-break-inside: avoid; }
        tr:nth-child(even) td { background-color: #F8FAFC; }
        th {
            background-color: #1E3A8A;
            color: #FFFFFF;
            font-weight: bold;
            font-size: 11.5pt;
        }
        .score-cell {
            text-align: center;
            font-weight: bold;
            color: #1E3A8A;
            background-color: #EFF6FF !important;
            font-size: 12pt;
            white-space: nowrap;
        }
        .ts-badge {
            display: inline-block;
            background-color: #E0F2FE;
            color: #0369A1;
            border: 1px solid #BAE6FD;
            padding: 1px 6px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 10.5pt;
            font-weight: bold;
        }
        ul { margin-top: 6px; margin-bottom: 8px; padding-right: 22px; }
        li { margin-bottom: 8px; page-break-inside: avoid; }
    </style>
    """

    if "<style>" in html_content:
        html_content = re.sub(r"<style>.*?</style>", executive_css, html_content, count=1, flags=re.DOTALL)
    elif "</head>" in html_content:
        html_content = html_content.replace("</head>", f"{executive_css}\n</head>", 1)
    else:
        html_content = f"<!DOCTYPE html><html dir='rtl' lang='ar'><head><meta charset='utf-8'>{executive_css}</head><body>{html_content}</body></html>"

    if 'class="header-banner"' not in html_content:
        html_content = re.sub(
            r"(<h1>.*?</h1>\s*(?:<div class=[\"']subtitle[\"']>.*?</div>)?)",
            r'<div class="header-banner">\1</div>',
            html_content,
            count=1,
            flags=re.DOTALL
        )

    html_content = re.sub(
        r"(<h2>\s*الخلاصة التنفيذية\s*</h2>\s*)(?!<div class=[\"']exec-box[\"']>)(<p>.*?</p>)",
        r'\1<div class="exec-box">\2</div>',
        html_content,
        flags=re.DOTALL
    )
    html_content = re.sub(
        r"(<h2>\s*الخلاصة النهائية\s*</h2>\s*)(?!<div class=[\"']exec-box[\"']>)(<p>.*?</p>)",
        r'\1<div class="exec-box">\2</div>',
        html_content,
        flags=re.DOTALL
    )
    html_content = re.sub(
        r"(<h2>\s*نقاط القوة[^<]*</h2>\s*)(?!<div class=[\"']strengths-box[\"']>)(<ul>.*?</ul>)",
        r'\1<div class="strengths-box">\2</div>',
        html_content,
        flags=re.DOTALL
    )
    html_content = re.sub(
        r"(<h2>\s*أولويات التطوير[^<]*</h2>\s*)(?!<div class=[\"']priorities-box[\"']>)(<ul>.*?</ul>)",
        r'\1<div class="priorities-box">\2</div>',
        html_content,
        flags=re.DOTALL
    )
    html_content = re.sub(
        r"<td>\s*(\d+(?:\.\d+)?\s*(?:/|من)\s*\d+(?:\.\d+)?)\s*</td>",
        r'<td class="score-cell">\1</td>',
        html_content
    )
    html_content = re.sub(
        r"(من\s+\d{2}:\d{2}(?::\d{2})?\s+إلى\s+\d{2}:\d{2}(?::\d{2})?)",
        r'<span class="ts-badge">\1</span>',
        html_content
    )

    cost_comment = f"<!-- COST_USD: {run_cost_usd:.4f} -->\n"
    return cost_comment + html_content


def save_bulletproof_backups(main_html_str, sum_html_str, summary_dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    main_path = os.path.join(BACKUP_DIR, f"01_Main_Detailed_Report_{CLEAN_NAME}.html")
    sum_path = os.path.join(BACKUP_DIR, f"02_Summarized_Report_{CLEAN_NAME}.html")
    json_path = os.path.join(BACKUP_DIR, "_run_summary.json")

    cost_val = summary_dict.get("processCost", summary_dict.get("process_cost_usd", 0.0))

    if main_html_str:
        with open(main_path, "w", encoding="utf-8") as f1:
            f1.write(beautify_report_html(main_html_str, cost_val))
    if sum_html_str:
        with open(sum_path, "w", encoding="utf-8") as f2:
            f2.write(beautify_report_html(sum_html_str, cost_val))
    if summary_dict:
        with open(json_path, "w", encoding="utf-8") as f3:
            json.dump(summary_dict, f3, ensure_ascii=False, indent=2)

    if main_html_str and sum_html_str:
        cap_base = f"📎 نسخة احتياطية ({CLEAN_NAME}) | التكلفة: ${cost_val:.4f}"
        send_telegram_document(main_path, caption=cap_base + " - التقرير التفصيلي")
        send_telegram_document(sum_path, caption=cap_base + " - التقرير المختصر")
        send_direct_smtp_email(
            subject=f"✅ اكتمل تقييم الحلقة: {CLEAN_NAME} (التكلفة: ${cost_val:.4f})",
            html_body=sum_html_str,
            attachment_paths=[main_path, sum_path]
        )


def log_step(msg):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)
    send_telegram(f"🔔 تحديث ({CLEAN_NAME}):\n⚙️ {MODE_TITLE}\n{msg[:300]}", use_html=False)


# ==============================================================================
# الفحص المسبق لجاهزية الحصة (Pre-Upload Probe) لمنع إعادة الترفيع عند 429
# ==============================================================================
def probe_key_before_upload(api_key, key_label):
    """يفحص المفتاح المجاني خلال نصف ثانية للتأكد من عدم استنفاد حصته قبل رفع الملفات."""
    try:
        url = f"[https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key=](https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key=){api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 1}
        }
        res = session.post(url, json=payload, timeout=8)
        if res.status_code == 200:
            return True, "READY"
        elif res.status_code in (429, 503):
            return False, f"HTTP {res.status_code} (Quota Exhausted)"
        return True, f"HTTP {res.status_code}"
    except Exception as e:
        return False, str(e)


def _find_spend_tracker_file_id():
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    q = f"name = '{SPEND_TRACKER_FILENAME}' and trashed = false"
    res = session.get("[https://www.googleapis.com/drive/v3/files](https://www.googleapis.com/drive/v3/files)", headers=headers, params={"q": q, "fields": "files(id, name)", "pageSize": 1}, timeout=20)
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
        res = session.get(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){file_id}?alt=media", headers=headers, timeout=20)
        if res.status_code == 200:
            data = res.json()
            if data.get("month") == current_month:
                return float(data.get("spent", 0.0))
    except Exception:
        pass
    return 0.0


def add_monthly_spend(amount):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    current_spent = get_monthly_spend()
    new_spent = round(current_spent + float(amount), 6)
    payload_bytes = json.dumps({"month": current_month, "spent": new_spent, "updated_at": datetime.now(timezone.utc).isoformat()}).encode("utf-8")
    try:
        file_id = _find_spend_tracker_file_id()
        if file_id:
            headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json; charset=utf-8"}
            session.patch(f"[https://www.googleapis.com/upload/drive/v3/files/](https://www.googleapis.com/upload/drive/v3/files/){file_id}?uploadType=media", headers=headers, data=payload_bytes, timeout=20)
        else:
            metadata = {"name": SPEND_TRACKER_FILENAME, "mimeType": "application/json"}
            boundary = "===boundary_spend_tracker==="
            body = f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{payload_bytes.decode('utf-8')}\r\n--{boundary}--".encode("utf-8")
            headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"multipart/related; boundary={boundary}"}
            session.post("[https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart](https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart)", headers=headers, data=body, timeout=30)
    except Exception:
        pass
    return new_spent


def get_model_rates(model_name, in_tokens):
    if "pro" in model_name.lower():
        in_rate = (1.25 / 1_000_000) if in_tokens <= 200_000 else (2.50 / 1_000_000)
        out_rate = (10.00 / 1_000_000) if in_tokens <= 200_000 else (15.00 / 1_000_000)
    else:
        in_rate = 0.15 / 1_000_000
        out_rate = 0.60 / 1_000_000
    return in_rate, out_rate


def calculate_call_cost(model_name, usage_metadata):
    in_tokens = int(usage_metadata.get("promptTokenCount", 0) or 0)
    out_tokens = int(usage_metadata.get("candidatesTokenCount", 0) or 0)
    think_tokens = int(usage_metadata.get("thoughtsTokenCount", 0) or 0)
    total_out = out_tokens + think_tokens
    in_rate, out_rate = get_model_rates(model_name, in_tokens)
    return max((in_tokens * in_rate) + (total_out * out_rate), 0.0001)


def get_prioritized_keys():
    """يرتب المفاتيح، ويفحص الحصة المجانية فوراً لتجاوز أي مفتاح مستنفد دون تضييع الوقت."""
    valid_free = []
    for idx, fk in enumerate(FREE_API_KEYS, start=1):
        is_ok, msg = probe_key_before_upload(fk, f"FREE (Key #{idx})")
        if is_ok:
            valid_free.append((idx, f"FREE (Key #{idx})", fk, False))
        else:
            log_step(f"⚡ المفتاح المجاني (Key #{idx}) مستنفد مسبقاً ({msg}) - تخطي فوري بدون ترفيع.")

    valid_paid = None
    if PAID_API_KEY:
        p_idx = len(FREE_API_KEYS) + 1
        valid_paid = (p_idx, f"PAID (Key #{p_idx})", PAID_API_KEY, True)

    if FORCE_FREE:
        if not valid_free:
            raise RuntimeError("المجلد مضبوط على الوضع المجاني (_free) ولا توجد مفاتيح مجانية صالحة حالياً.")
        return valid_free

    ordered = list(valid_free)
    if valid_paid:
        ordered.append(valid_paid)

    if not ordered:
        raise RuntimeError("لا توجد أي مفاتيح Gemini صالحة في الإعدادات.")
    return ordered


def update_drive_folder_and_summary(folder_id, new_folder_name, run_cost_usd, monthly_spent_usd, model_used, thinking_level, duration_str, local_output_dir):
    """يكتب التكلفة في _run_summary.json ووصف المجلد قبل تغيير اسم المجلد إلى PROCESSED_ لمنع ظهور الصفر ($0.00)."""
    summary_data = {
        "status": "SUCCESS",
        "process_cost_usd": round(float(run_cost_usd), 4),
        "total_cost_usd": round(float(run_cost_usd), 4),
        "monthly_spend_usd": round(float(monthly_spent_usd), 4),
        "model_used": model_used,
        "thinking_level": thinking_level,
        "duration_hms": duration_str
    }

    summary_path = os.path.join(local_output_dir, "_run_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)

    # رفع _run_summary.json للدرايف
    existing = session.get(
        "[https://www.googleapis.com/drive/v3/files](https://www.googleapis.com/drive/v3/files)",
        headers={"Authorization": f"Bearer {DRIVE_TOKEN}"},
        params={"q": f"'{folder_id}' in parents and name = '_run_summary.json' and trashed = false", "fields": "files(id)"},
        timeout=20
    ).json().get("files", [])
    for item in existing:
        session.delete(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){item['id']}", headers={"Authorization": f"Bearer {DRIVE_TOKEN}"}, timeout=15)

    with open(summary_path, "rb") as sf:
        session.post(
            "[https://www.googleapis.com/upload/drive/v3/files?uploadType=media](https://www.googleapis.com/upload/drive/v3/files?uploadType=media)",
            headers={"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json"},
            data=sf.read(),
            timeout=30
        )

    desc_text = (
        f"COST_USD: ${run_cost_usd:.4f} | "
        f"MONTH_USD: ${monthly_spent_usd:.4f} | "
        f"MODEL: {model_used} ({thinking_level}) | "
        f"DURATION: {duration_str}"
    )
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json"}
    session.patch(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){folder_id}", headers=headers, json={"name": new_folder_name, "description": desc_text}, timeout=25)


def upload_report_to_drive(filename, content_str, mime_type="text/html"):
    q = f"'{FOLDER_ID}' in parents and name = '{filename}' and trashed = false"
    existing_id = None
    try:
        r_list = session.get("[https://www.googleapis.com/drive/v3/files](https://www.googleapis.com/drive/v3/files)", headers={"Authorization": f"Bearer {DRIVE_TOKEN}"}, params={"q": q, "fields": "files(id)"}, timeout=20)
        if r_list.status_code == 200:
            files = r_list.json().get("files", [])
            if files:
                existing_id = files[0]["id"]
    except Exception:
        pass

    if existing_id:
        session.patch(
            f"[https://www.googleapis.com/upload/drive/v3/files/](https://www.googleapis.com/upload/drive/v3/files/){existing_id}?uploadType=media",
            headers={"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"{mime_type}; charset=UTF-8"},
            data=content_str.encode("utf-8"),
            timeout=45
        )
    else:
        metadata = {"name": filename, "parents": [FOLDER_ID], "mimeType": mime_type}
        boundary = "===boundary_report==="
        body = f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}; charset=UTF-8\r\n\r\n{content_str}\r\n--{boundary}--".encode("utf-8")
        session.post("[https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart](https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart)", headers={"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"multipart/related; boundary={boundary}"}, data=body, timeout=45)


def list_drive_folder_contents():
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    res = session.get("[https://www.googleapis.com/drive/v3/files](https://www.googleapis.com/drive/v3/files)", headers=headers, params={"q": f"'{FOLDER_ID}' in parents and trashed = false", "fields": "files(id, name, mimeType, size)"}, timeout=30)
    res.raise_for_status()
    return res.json().get("files", [])


def extract_url_from_text(text):
    if not text:
        return None
    for m in re.finditer(r'(https?://[^\s<>"\'\r\n]+)', text):
        candidate = m.group(1).strip().rstrip('.,;)]}')
        if "[drive.google.com/drive/folders/](https://drive.google.com/drive/folders/)" not in candidate:
            return candidate
    return None


def find_video_url_in_folder(all_files, folder_desc=""):
    global ORIGINAL_SOURCE_URL
    if folder_desc:
        url = extract_url_from_text(folder_desc)
        if url:
            ORIGINAL_SOURCE_URL = url
            return url, "وصف المجلد"

    for f in all_files:
        fname = f.get("name", "")
        if fname.startswith("00_") or fname.startswith("_") or fname.endswith(".html"):
            continue
        try:
            if f.get("mimeType") == "application/vnd.google-apps.document":
                r = session.get(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){f['id']}/export", headers={"Authorization": f"Bearer {DRIVE_TOKEN}"}, params={"mimeType": "text/plain"}, timeout=20)
                if r.status_code == 200:
                    url = extract_url_from_text(r.text)
                    if url:
                        ORIGINAL_SOURCE_URL = url
                        return url, f"Google Doc ({fname})"
            elif f.get("mimeType", "").startswith("text/") or fname.endswith((".txt", ".md")):
                r = session.get(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){f['id']}?alt=media", headers={"Authorization": f"Bearer {DRIVE_TOKEN}"}, timeout=20)
                if r.status_code == 200:
                    url = extract_url_from_text(r.text)
                    if url:
                        ORIGINAL_SOURCE_URL = url
                        return url, f"ملف نصي ({fname})"
        except Exception:
            pass
    return None, None


def download_drive_file(file_id, local_path):
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    with session.get(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){file_id}?alt=media", headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)


def download_video_from_external_url(url, local_path):
    gdrive_match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]{20,})', url)
    if gdrive_match and "drive.google.com" in url:
        try:
            download_drive_file(gdrive_match.group(1), local_path)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
                return local_path
        except Exception:
            pass

    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=False)
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-check-certificates", "-f", "best[height<=720][ext=mp4]/best", "-o", local_path, url]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
            return local_path
    except Exception as e:
        raise RuntimeError(f"فشل سحب الفيديو من الرابط: {e}")


def get_duration_seconds(filepath):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    out = subprocess.check_output(cmd).decode("utf-8").strip()
    return float(out)


def format_hhmmss(seconds):
    s = int(round(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def prepare_media_segments(drive_files, external_url=None, url_source=None):
    local_raw = "raw_episode.mp4"
    if external_url:
        log_step(f"🔗 سحب الحلقة من {url_source}: {external_url}")
        media_input = download_video_from_external_url(external_url, local_raw)
    elif len(drive_files) == 1 and drive_files[0]["mimeType"].startswith("video/"):
        download_drive_file(drive_files[0]["id"], local_raw)
        media_input = local_raw
    else:
        raise RuntimeError("لم يتم العثور على ملف فيديو صالح أو رابط معالجة في المجلد.")

    total_sec = get_duration_seconds(media_input)
    exact_duration = format_hhmmss(total_sec)
    log_step(f"⏱️ المدة الفعلية للحلقة: {exact_duration}")

    subprocess.run(["ffmpeg", "-y", "-i", media_input, "-vn", "-c:a", "libmp3lame", "-ac", "1", "-ar", "24000", "-b:a", "64k", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Audio_part%02d.mp3"], check=True)
    subprocess.run(["ffmpeg", "-y", "-i", media_input, "-an", "-c:v", "libx264", "-preset", "faster", "-crf", "30", "-maxrate", "150k", "-bufsize", "300k", "-vf", "scale=-2:720,fps=1", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Visuals_part%02d.mp4"], check=True)

    return sorted(glob.glob("Audio_part*.mp3")) + sorted(glob.glob("Visuals_part*.mp4")), exact_duration


def upload_to_gemini_file_api(filepath, api_key):
    mime = "audio/mp3" if filepath.endswith(".mp3") else "video/mp4"
    filesize = os.path.getsize(filepath)
    init_url = f"[https://generativelanguage.googleapis.com/upload/v1beta/files?key=](https://generativelanguage.googleapis.com/upload/v1beta/files?key=){api_key}"
    r = session.post(init_url, headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(filesize), "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json"}, json={"file": {"display_name": filepath}}, timeout=30)
    r.raise_for_status()
    upload_url = r.headers["X-Goog-Upload-URL"]

    with open(filepath, "rb") as f:
        data = f.read()
        up_res = session.post(upload_url, headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "upload, finalize", "X-Goog-Upload-Offset": "0", "Content-Length": str(filesize)}, data=data, timeout=180)
        up_res.raise_for_status()

    file_info = up_res.json()["file"]
    for _ in range(60):
        if file_info.get("state") == "ACTIVE":
            return file_info["uri"], mime
        time.sleep(3)
        chk = session.get(f"{file_info['uri']}?key={api_key}", timeout=20).json()
        file_info["state"] = chk.get("state")
    raise RuntimeError(f"تخلف الملف {filepath} عن أن يصبح ACTIVE.")


def evaluate_with_gemini(segments, exact_duration):
    models = [
        ("gemini-3.1-pro-preview", 24576, "gemini-3.1-pro-preview (Max Thinking 24576)", True),
        ("gemini-3.8-flash", 24576, "gemini-3.8-flash (High Thinking 24576)", False),
        ("gemini-3.8-flash", 0, "gemini-3.8-flash (Standard Fast)", False),
    ]

    system_prompt = (
        f"{SKILL_INSTRUCTIONS_AND_RUBRIC}\n"
        f"مدة الحلقة الإجمالية: {exact_duration}. أرجع كائن JSON بالحقول: isFullDurationAnalyzed, analyzedAllSegments, lastSpokenWords, mainDetailedHtml, summarizedHtml."
    )

    prioritized_keys = get_prioritized_keys()
    errors = []

    for key_idx, key_label, api_key, is_paid_key in prioritized_keys:
        log_step(f"🔑 تجربة المفتاح {key_label}...")
        try:
            prompt_parts = []
            for seg in segments:
                log_step(f"☁️ رفع الجزء ({seg}) عبر {key_label}...")
                uri, mime = upload_to_gemini_file_api(seg, api_key)
                prompt_parts.append({"text": f"الملف المرفق: {seg}"})
                prompt_parts.append({"fileData": {"mimeType": mime, "fileUri": uri}})
        except Exception as up_err:
            errors.append(f"• {key_label}: فشل الرفع ({up_err})")
            continue

        for model_name, budget, label, is_highest in models:
            log_step(f"🧠 تحليل عبر النموذج: {label} ({key_label})...")
            gen_cfg = {"temperature": 0.0, "maxOutputTokens": 65536, "responseMimeType": "application/json"}
            if budget > 0:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}

            payload = {"systemInstruction": {"parts": [{"text": system_prompt}]}, "contents": [{"role": "user", "parts": prompt_parts + [{"text": f"قيم حلقة {CLEAN_NAME} بالكامل وفق السلم المعتمد."}]}], "generationConfig": gen_cfg}
            try:
                res = session.post(f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){model_name}:generateContent?key={api_key}", json=payload, timeout=500)
                if res.status_code == 200:
                    data = res.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(text)

                    process_cost = calculate_call_cost(model_name, data.get("usageMetadata", {})) if is_paid_key else 0.0
                    total_monthly = add_monthly_spend(process_cost) if is_paid_key else get_monthly_spend()

                    parsed["costInfo"] = {
                        "keyLabel": key_label,
                        "modelUsed": label,
                        "isPaid": is_paid_key,
                        "processCost": round(process_cost, 4),
                        "totalMonthly": round(total_monthly, 4)
                    }
                    return parsed
                elif res.status_code == 429:
                    log_step(f"⚡ المفتاح مستنفد ({key_label}) - انتقال فوري للمفتاح التالي.")
                    break
            except Exception as e:
                errors.append(f"{key_label} [{label}]: {e}")
                break

    raise RuntimeError("\n".join(errors))


def main():
    try:
        log_step(f"انطلاق المعالجة: {CLEAN_NAME} | {MODE_TITLE}")
        _ = get_prioritized_keys()

        folder_meta = session.get(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){FOLDER_ID}", headers={"Authorization": f"Bearer {DRIVE_TOKEN}"}, params={"fields": "id,name,description"}, timeout=25).json()
        all_files = list_drive_folder_contents()
        drive_files = [f for f in all_files if f.get("mimeType", "").startswith("video/") or f.get("mimeType", "").startswith("audio/")]

        external_url, url_source = None, None
        if not drive_files:
            external_url, url_source = find_video_url_in_folder(all_files, folder_meta.get("description", ""))
            if not external_url:
                raise RuntimeError("لم يتم العثور على أي رابط فيديو أو ملف مرفق في المجلد.")

        segments, exact_duration = prepare_media_segments(drive_files, external_url, url_source)
        result = evaluate_with_gemini(segments, exact_duration)

        cost_info = result.get("costInfo", {"keyLabel": "FREE", "modelUsed": "gemini-3.8-flash", "isPaid": False, "processCost": 0.0, "totalMonthly": get_monthly_spend()})
        main_html = beautify_report_html(result["mainDetailedHtml"], cost_info["processCost"])
        sum_html = beautify_report_html(result["summarizedHtml"], cost_info["processCost"])

        last_spoken = result.get("lastSpokenWords", "غير متوفر").strip()
        elapsed = int(time.time() - START_TIME)
        elapsed_str = f"{elapsed // 60} دقيقة و {elapsed % 60} ثانية"

        summary_json_data = {
            "status": "SUCCESS",
            "folder_name": CLEAN_NAME,
            "mode_title": MODE_TITLE,
            "model_used": cost_info.get("modelUsed", ""),
            "key_label": cost_info["keyLabel"],
            "process_cost_usd": cost_info["processCost"],
            "episode_budget_usd": EPISODE_MAX_COST,
            "monthly_spend_usd": cost_info["totalMonthly"],
            "monthly_cap_usd": MAX_MONTHLY_BUDGET,
            "duration_hms": exact_duration,
            "last_spoken_words": last_spoken
        }

        # تأمين النسخ الاحتياطية وإرسال الإيميل المباشر والتليغرام قبل لمس Drive
        save_bulletproof_backups(main_html, sum_html, summary_json_data)

        # رفع الملفات للدرايف
        upload_report_to_drive(f"01_Main_Detailed_Report_{CLEAN_NAME}.html", main_html, "text/html")
        upload_report_to_drive(f"02_Summarized_Report_{CLEAN_NAME}.html", sum_html, "text/html")

        # تحديث المجلد وإعطاء الإشارة لـ Code.gs مع تمرير التكلفة الصحيحة
        update_drive_folder_and_summary(
            FOLDER_ID,
            f"PROCESSED_{CLEAN_NAME}",
            cost_info["processCost"],
            cost_info["totalMonthly"],
            cost_info.get("modelUsed", ""),
            "Max",
            exact_duration,
            "."
        )

        send_telegram(f"✅ تمت معالجة وتقييم الحلقة بنجاح!\n📌 {CLEAN_NAME}\n💵 التكلفة: ${cost_info['processCost']:.4f}", use_html=False)
    except Exception as e:
        error_msg = str(e)
        log_step(f"❌ خطأ: {error_msg}")
        fail_summary = {"status": "FAILED", "error_reason": error_msg}
        save_bulletproof_backups(None, None, fail_summary)
        session.patch(f"[https://www.googleapis.com/drive/v3/files/](https://www.googleapis.com/drive/v3/files/){FOLDER_ID}", headers={"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json"}, json={"name": f"FAILED_{CLEAN_NAME}"}, timeout=20)
        send_telegram(f"❌ فشل المعالجة: {CLEAN_NAME}\nالسبب: {error_msg[:1000]}", use_html=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
