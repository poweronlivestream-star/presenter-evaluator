#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
منظومة تقييم مقدمي البرامج التلفزيونية المحصنة (evaluate.py) - النسخة النهائية الكاملة:
- إذا بدأ المجلد بـ READY_GEN_ أو GEN_READY_ (أو تضمن _gen أو _neutral): يشغّل [المنظومة الثالثة: التقييم المهني العام والمحايد بدون خلفية قناة موجهة].
- إذا بدأ المجلد بـ READY_ العادية: يشغّل [المنظومة الثانية: التقييم مع مراعاة الخط التحريري للقناة الموجهة].
- سحب مضمون 100% للرابط من وصف المجلد (Folder Description) ومن ملف _source_link.txt أو Google Doc أو ملف نصي.
- دعم كامل لروابط يوتيوب (عبر yt-dlp المطور + التمرير المباشر لخوادم Gemini عند حظر IP السحابة).
- فحص لحظي (0.5 ثانية - 1 توكن) للكوتا والميزانية قبل رفع أي ملف لمنع إضاعة الوقت في الرفع المتكرر.
- تثبيت قاعدة اللهجة اللبنانية المحكية + التصميم التنفيذي الملون (Royal Executive RTL Theme) + النسخ الاحتياطي الرباعي.
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

# اكتشاف المنظومة الثالثة (التقييم المهني العام والمحايد - بدون خلفية قناة موجهة)
IS_GENERAL_NEUTRAL_MODE = bool(
    UPPER_NAME.startswith("GEN_")
    or UPPER_NAME.startswith("NEUTRAL_")
    or "_GEN_" in UPPER_NAME
    or UPPER_NAME.endswith("_GEN")
    or "_NEUTRAL_" in UPPER_NAME
    or UPPER_NAME.endswith("_NEUTRAL")
)

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

EDITORIAL_TYPE_LABEL = (
    "المنظومة الثالثة: تقييم مهني إعلامي عام ومحايد (بدون خلفية قناة موجهة)"
    if IS_GENERAL_NEUTRAL_MODE
    else "المنظومة الثانية: تقييم مع مراعاة الخط التحريري للقناة الموجهة"
)

if FORCE_FREE and IS_LOW_MODE:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | مجاني اقتصادي (_LOW_FREE)"
elif FORCE_FREE:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | مجاني حصري (_FREE)"
elif FORCE_PAID and IS_LOW_MODE:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | مدفوع اقتصادي (_LOW_PAID - {EPISODE_COST_SOURCE})"
elif IS_PAID_MIN_COST:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | أقل تكلفة ممكنة (_PAID | {EPISODE_COST_SOURCE})"
elif FORCE_PAID:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | مدفوع متدرج ({EPISODE_COST_SOURCE})"
elif IS_LOW_MODE:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | اقتصادي (_LOW | {EPISODE_COST_SOURCE})"
else:
    MODE_TITLE = f"{EDITORIAL_TYPE_LABEL} | احترافي متدرج ({EPISODE_COST_SOURCE})"

# ==============================================================================
# البند الثالث المتغير حسب نوع المنظومة (المنظومة الثانية الموجهة vs المنظومة الثالثة العامة)
# ==============================================================================
EDITORIAL_RULE_DIRECTED = """3. مراعاة الخط التحريري وسياق القناة (قاعدة جوهرية لا يجوز مخالفتها):
   - يُجرى التقييم مع الأخذ في الحسبان الهوية التحريرية للقناة (إعلام داعم ومتبنٍّ لخط المقاومة).
   - يُمنع منعاً باتاً خصم علامات أو انتقاد المقدم بسبب تبنيه لخط المقاومة أو استخدامه مصطلحاتها (مثل: كيان العدو، المقاومة، الشهيد، الاعتداءات الإسرائيلية) أو عدم لعبه دور "المحايد" تجاه العدو؛ فهذا التزام طبيعي بالسياسة التحريرية للقناة وليس عيباً مهنياً.
   - التمايز بين "المونولوج العبثي" و"التوظيف التحريري الهادف": إذا كان الضيف حليفاً وفي مسار تفكيك لسرديات خصوم المقاومة، فإن ترك المساحة له قد يكون قراراً تحريرياً مقصوداً.
   - حدود التوظيف التحريري: المونولوج الذي يتجاوز 3 دقائق متصلة دون أي مقاطعة وظيفية أو سؤال تلخيصي أو توجيهي من المقدم يُعد فشلاً تقنياً في إدارة الإيقاع التلفزيوني (Broken TV Rhythm)، ويجب المحاسبة عليه بخصم النقاط في معايير "التعامل مع الضيف"، "المتابعة والتعقيب"، و"ضبط الوقت".
   - في معيار "الاختصار والحياد وتجنب تعدد الأسئلة"، يُقصد بالحياد المهني هنا: عدم مصادرة رأي الضيف بمقدمات تقريرية مطولة تفرض عليه الإجابة، وليس الحياد السياسي تجاه العدو."""

EDITORIAL_RULE_GENERAL_NEUTRAL = """3. المعيار المهني الإعلامي العام والحياد الحواري (تقييم غير موجّه - بدون خلفية قناة موجهة):
   - يُجرى التقييم وفق المعايير المهنية التلفزيونية العامة والمستقلة، دون افتراض وجود خط أيديولوجي أو سياسي موجّه للقناة، ودون منح أي استثناء أيديولوجي للمقدم أو الضيف.
   - يُحاسب المقدم مهنياً على التزامه بمبدأ المسافة المهنية الواحدة، وعدم الانحياز الشخصي أو تبني سردية الضيف أو التسليم بمقدماته دون تمحيص.
   - يُطلب من المقدم لعب دور "المحاور المسائل والمدقق" (Critical Interviewer) مع جميع الضيوف بلا استثناء؛ فيُحاسب على مدى مواجهته للضيف بالأسئلة الاستقصائية والتدقيق في التناقضات ومقاطعته مهنياً عند التهرب من السؤال أو إلقاء سرديات إنشائية.
   - أي استرسال أو مونولوج للضيف يتجاوز دقيقتين إلى 3 دقائق متصلة دون مقاطعة وظيفية أو توجيه أو استجواب من المقدم يُعدّ ضعفاً تقنياً ومهنياً في السيطرة على الاستوديو وإدارة الإيقاع التلفزيوني، ويُحاسب عليه بصرامة في معايير "التعامل مع الضيف"، "المتابعة والتعقيب"، و"ضبط الوقت".
   - في معيار "الاختصار والحياد وتجنب تعدد الأسئلة"، يُقصد بالحياد المهني: الحياد الكامل في صياغة السؤال (عدم تضمين السؤال أحكاماً مسبقة أو مقدمات توجيهية منحازة أو مصادرة لإجابة الضيف)."""

SELECTED_EDITORIAL_RULE = EDITORIAL_RULE_GENERAL_NEUTRAL if IS_GENERAL_NEUTRAL_MODE else EDITORIAL_RULE_DIRECTED
METHODOLOGY_TEXT_NOTE = (
    "التقييم مهني وتقني مستقل وفق المعايير الإعلامية العامة والحياد الحواري (بدون خلفية قناة موجهة) مع مراعاة قاعدة المزاوجة اللغوية المحببة باللهجة اللبنانية"
    if IS_GENERAL_NEUTRAL_MODE
    else "التقييم مهني وتقني مع مراعاة الخط التحريري للقناة وقاعدة المزاوجة اللغوية المحببة باللهجة اللبنانية"
)

SKILL_INSTRUCTIONS_AND_RUBRIC = f"""
أنت خبير متخصص في تقييم الأداء المهني والتلفزيوني لمقدمي البرامج السياسية والحوارية وفق السلم المعتمد (90 نقطة).
نوع التقييم المطلوب في هذه الحلقة: [{EDITORIAL_TYPE_LABEL}].
يجب عليك الالتزام الحرفي والصارم بنسبة 100% بجميع القواعد والمعايير والقوالب التالية دون أي اختصار أو اجتهاد خارجها:

================================================================================
أولاً: القواعد الإلزامية الصارمة (PROJECT INSTRUCTIONS)
================================================================================
1. المنع المطلق للتخمين (Zero Hallucination Policy):
   - يُمنع منعاً باتاً توقع أو استنتاج أو تخمين أي معلومة غير موجودة صراحة في الفيديو أو الصوت المرفق (سواء كانت اسم مقدم، اسم ضيف، صفة ضيف، تاريخ، اسم برنامج، توقيت، أو اقتباس).
   - إذا كانت أي معلومة أساسية غير مذكورة صراحة في المقطع، يُكتب مكانها "غير محدد في المقطع".
   - تُبنى كافة البيانات والملاحظات والعلامات بنسبة 100% من محتوى الفيديو/الصوت الخاص بالحلقة الحالية حصراً.

2. التحقق الإلزامي من مدة الحلقة وتحليلها بالكامل (Full Duration Verification):
   - يجب تحليل الحلقة بالكامل من الثانية الأولى (00:00:00) وحتى آخر ثانية في المقطع الأخير دون أي انقطاع أو قفز أو أخذ عينات.
   - التوقيت يكتب بصيغة واضحة ومتصلة من بداية الحلقة: `من 00:08:24 إلى 00:08:28` لمنع الانعكاس في RTL. ممنوع نهائياً كتابة "الجزء الأول" أو "المقطع الثاني".

{SELECTED_EDITORIAL_RULE}

4. قاعدة اللهجة اللبنانية المحكية والمزاوجة اللغوية (قاعدة إلزامية صارمة لا يجوز مخالفتها):
   - استخدام اللهجة العامية اللبنانية في الحوار ليس أمراً سلبياً إطلاقاً: يُمنع منعاً باتاً خصم أي علامة أو توجيه أي انتقاد سلبي أو إدراج ضمن "أولويات التطوير" بسبب استخدام المقدم لعبارات أو أسئلة أو مداخلات باللهجة اللبنانية المحكية أثناء الحوار.
   - التطعيم المتوازن باللهجة اللبنانية المحكية يُعتبر أسلوباً تواصلياً محبباً وطبيعياً يكسر الجمود والرتابة ("البيئة الناشفة" للفصحى الصرفة) ويخلق جاذبية وقرباً من المشاهد.
   - في معيار "اللغة والطلاقة (2 نقطة)"، تقتصر المحاسبة حصراً على: سلامة النطق ومخارج الحروف، طلاقة اللسان وعدم التلعثم أو التردد المربك، وتجنب الأخطاء النحوية أو اللغوية الفادحة عند التحدث بالفصحى.

5. العمق التحليلي والشواهد الحية الموثقة (إلزامي في التقرير المفصل):
   - يُمنع منعاً باتاً الاكتفاء بعبارات عامة أو ملخصات قصيرة! يجب أن تكون الملاحظات المهنية داخل الجداول غنية، ناقدة، ومفصلة، ومدعومة بـ:
     أ) اقتباسات نصية حرفية (Verbatim Quotes) من كلام المقدم مع توقيتها الدقيق (مثلاً: صياغة الأسئلة الطويلة/المركبة، الأسئلة التتبعية الذكية، المقاطعات الوظيفية، أو التلعثم اللغوي إن وجد).
     ب) رصد بصري دقيق للغة الجسد والحضور التلفزيوني من ملفات الفيديو مع التوقيت الدقيق بالثانية (مثل: استقامة الجلسة، توزيع النظرات بين الضيف والكاميرا، حركة اليدين، العادات الحركية السلبية كلمس الوجه أو الذقن أو العبث باللحية، التلويح بالقلم، وتعابير الوجه أثناء الإصغاء).
     ج) تحليل دقيق لإدارة الوقت والانتقالات بين ضيوف الاستوديو والمراسلين الميدانيين أو التقارير المسجلة (VT).

================================================================================
ثانياً: سلم العلامات المعتمد حصرياً (SCORING RUBRIC — 90 نقطة)
================================================================================
يُمنع منعاً باتاً اختراع معايير أخرى أو تغيير أوزانها! التزم حصراً بهذه المعايير الستة الأساسية ومعاييرها الفرعية الـ 23:

1) إدارة الحلقة — 20 نقطة:
   - بناء وتسلسل الملفات: 5
   - إغلاق الملف والانتقال: 5
   - السيطرة على المسار والمرونة: 5
   - الافتتاح والخاتمة: 5

2) التعامل مع الضيف — 20 نقطة:
   - الإصغاء واحترام الجواب: 5
   - توزيع المساحة وعدم المقاطعة غير الضرورية: 5
   - المقاطعة الضرورية ومواجهة الضيف: 5
   - احتواء المونولوج وإعادة المسار: 5

3) جودة الأسئلة — 20 نقطة:
   - وضوح السؤال: 4
   - دقة السؤال: 4
   - عمق السؤال: 4
   - الارتباط بالمحور: 4
   - الاختصار والحياد وتجنب تعدد الأسئلة: 4

4) المتابعة والتعقيب — 15 نقطة:
   - المتابعة المبنية على الجواب: 4
   - التقاط النقاط المهمة والتناقضات: 4
   - طلب التوضيح والتحقق: 3.5
   - بناء السؤال التالي من الجواب: 3.5

5) الحضور والأداء — 10 نقاط:
   - الجاذبية التلفزيونية: 3
   - لغة الجسد والتواصل البصري: 2.5
   - الصوت والإلقاء: 2.5
   - اللغة والطلاقة: 2

6) ضبط الوقت — 5 نقاط:
   - توزيع الوقت بين الملفات والضيوف: 1.7
   - منع استنزاف الوقت في نقطة واحدة: 1.6
   - إنهاء المحاور والخاتمة: 1.7

(قاعدة العلامة: لا تجعل الانطباع العام يسبق الأدلة. الأدلة ← التصنيف ← العلامة. يجب أن يتطابق مجموع العلامات الفرعية مع علامة كل معيار أساسي، ومجموع المعايير الستة مع العلامة الإجمالية من 90).

================================================================================
ثالثاً: القالب الإلزامي الحرفي لمخرجات الـ HTML (Royal Executive Theme)
================================================================================
1. يجب أن يحتوي حقل `mainDetailedHtml` على كود HTML الكامل لـ "التقرير الرئيسي المفصل" مطابقاً تماماً لهذا الهيكل وجميع فئات التنسيق الملونة (`header-banner`, `exec-box`, `summary-strip`, `core-grid`, `score-cell`, `strengths-box`, `priorities-box`):

<div class="header-banner">
    <h1>تقييم تفصيلي وفق المعايير الموسعة</h1>
    <div class="subtitle">[اسم المقدم] - [الشهر والسنة / عنوان الحلقة]</div>
</div>

<table class="info-table">
    <tr><td>المقدم</td><td>[اسم المقدم]</td></tr>
    <tr><td>الحلقة</td><td>[نوع الحلقة / البرنامج - التاريخ]</td></tr>
    <tr><td>معيار التقييم</td><td>{EDITORIAL_TYPE_LABEL}</td></tr>
    <tr><td>الضيوف</td><td>[أسماء الضيوف والمراسلين وطريقة المشاركة: استديو / Zoom / مداخلة ميدانية]</td></tr>
    <tr><td>المادة المسجلة</td><td>[تفاصيل التقارير VT أو المداخلات وتوقيتها من 00:00:00 إلى 00:00:00 أو لا يوجد]</td></tr>
    <tr><td>مدة الحلقة</td><td>[المدة الدقيقة الكاملة بالثواني والدقائق - مع تأكيد تحليلها بالكامل]</td></tr>
</table>

<h2>الخلاصة التنفيذية</h2>
<div class="exec-box">
    <p>[فقرة تحليلية مكثفة ومعمقة من 5-8 أسطر تلخص المستوى العام للمقدم في الحلقة، أبرز ملامح الأداء في إدارة الحوار والأسئلة والمتابعة والحضور البصري واللغوي، ونقطة الضعف أو الاستنزاف الزمني الأساسية]</p>
</div>

<table class="summary-strip">
    <tr>
        <td><span class="label">العلامة</span><span class="val">[ ] من 90</span></td>
        <td><span class="label">النسبة</span><span class="val">[ ]%</span></td>
        <td><span class="label">أبرز نقطة قوة</span><span class="val">[توصيف مركز]</span></td>
        <td><span class="label">أولوية التطوير</span><span class="val">[توصيف مركز]</span></td>
    </tr>
</table>

<h2>منهج التقييم</h2>
<p>تم تطبيق السلم المعتمد: إدارة الحلقة 20، التعامل مع الضيف 20، جودة الأسئلة 20، المتابعة والتعقيب 15، الحضور والأداء 10، وضبط الوقت 5. {METHODOLOGY_TEXT_NOTE}، وجرى استخدام التفريغ الموقّت والفيديو للتحقق من الانتقالات والحضور البصري على كامل مدة الحلقة.</p>

<h2>العلامات الأساسية</h2>
<table class="core-grid">
    <tr>
        <td>إدارة الحلقة: [ ] من 20</td>
        <td>التعامل مع الضيف: [ ] من 20</td>
        <td>جودة الأسئلة: [ ] من 20</td>
    </tr>
    <tr>
        <td>المتابعة والتعقيب: [ ] من 15</td>
        <td>الحضور والأداء: [ ] من 10</td>
        <td>ضبط الوقت: [ ] من 5</td>
    </tr>
</table>

<h2>1. إدارة الحلقة - [ ]/20</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>بناء وتسلسل الملفات</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>إغلاق الملف والانتقال</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>السيطرة على المسار والمرونة</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>الافتتاح والخاتمة</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
</table>

<h2>2. التعامل مع الضيف - [ ]/20</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>الإصغاء واحترام الجواب</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>توزيع المساحة وعدم المقاطعة غير الضرورية</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>المقاطعة الضرورية ومواجهة الضيف</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>احتواء المونولوج وإعادة المسار</td><td class="score-cell">[ ]/5</td><td>[تحليل مفصل بالتوقيتات]</td></tr>
</table>

<h2>3. جودة الأسئلة - [ ]/20</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>وضوح السؤال</td><td class="score-cell">[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>دقة السؤال</td><td class="score-cell">[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>عمق السؤال</td><td class="score-cell">[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>الارتباط بالمحور</td><td class="score-cell">[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>الاختصار والحياد وتجنب تعدد الأسئلة</td><td class="score-cell">[ ]/4</td><td>[تحليل طول الأسئلة والحياد والأسئلة المركبة مع ذكر مدتها وتوقيتها واقتباسها]</td></tr>
</table>

<h2>4. المتابعة والتعقيب - [ ]/15</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>المتابعة المبنية على الجواب</td><td class="score-cell">[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>التقاط النقاط المهمة والتناقضات</td><td class="score-cell">[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>طلب التوضيح والتحقق</td><td class="score-cell">[ ]/3.5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>بناء السؤال التالي من الجواب</td><td class="score-cell">[ ]/3.5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
</table>

<h2>5. الحضور والأداء - [ ]/10</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>الجاذبية التلفزيونية</td><td class="score-cell">[ ]/3</td><td>[تحليل مفصل للهيئة والحضور والاتزان على الشاشة]</td></tr>
    <tr><td>لغة الجسد والتواصل البصري</td><td class="score-cell">[ ]/2.5</td><td>[رصد بصري دقيق بالتوقيتات لحركة اليدين، النظرات، لمس الوجه/اللحية، استخدام القلم، والجلسة]</td></tr>
    <tr><td>الصوت والإلقاء</td><td class="score-cell">[ ]/2.5</td><td>[تحليل الخامة الصوتية، التنغيم، مخارج الحروف، والنفس بالتوقيتات]</td></tr>
    <tr><td>اللغة والطلاقة</td><td class="score-cell">[ ]/2</td><td>[تقييم سلامة الفصحى وطلاقة اللسان ومرونة المزاوجة التواصلية المحببة باللهجة اللبنانية المحكية التي تكسر الجمود، مع المحاسبة فقط على التلعثم أو الأخطاء النحوية الفادحة عند التحدث بالفصحى بالتوقيتات]</td></tr>
</table>

<h2>6. ضبط الوقت - [ ]/5</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>توزيع الوقت بين الملفات والضيوف</td><td class="score-cell">[ ]/1.7</td><td>[تحليل مفصل لتوزيع الزمن بين المحاور والضيوف والمراسلين]</td></tr>
    <tr><td>منع استنزاف الوقت في نقطة واحدة</td><td class="score-cell">[ ]/1.6</td><td>[تحليل مفصل مدعوم بالتوقيتات]</td></tr>
    <tr><td>إنهاء المحاور والخاتمة</td><td class="score-cell">[ ]/1.7</td><td>[تحليل دقة إغلاق الحلقة في التوقيت الختامي]</td></tr>
</table>

<h2>أمثلة موثقة من الحلقة</h2>
<table>
    <tr><th style="width: 22%;">التوقيت</th><th style="width: 33%;">المثال (اقتباس حرفي أو رصد بصري)</th><th style="width: 45%;">الدلالة المهنية</th></tr>
    <tr><td>من 00:00:00 إلى 00:00:00</td><td>[وصف الموقف أو الاقتباس الحرفي]</td><td>[تحليل الدلالة المهنية إيجاباً أو سلباً وربطها بالمعيار]</td></tr>
    <!-- أدرج ما لا يقل عن 10 أمثلة موثقة موزعة على كامل مدة الحلقة من بدايتها لختامها -->
</table>

<h2>نقاط القوة الرئيسية</h2>
<div class="strengths-box">
    <ul>
        <li>[نقطة قوة مفصلة 1 مع التوقيت]</li>
        <li>[نقطة قوة مفصلة 2 مع التوقيت]</li>
        <li>[نقطة قوة مفصلة 3 مع التوقيت]</li>
        <li>[نقطة قوة مفصلة 4 مع التوقيت]</li>
    </ul>
</div>

<h2>أولويات التطوير</h2>
<div class="priorities-box">
    <ul>
        <li>[أولوية تطوير عملية 1 مع الشاهد الزمني - يُمنع إدراج اللهجة اللبنانية المحكية كعيب أو أولوية تطوير]</li>
        <li>[أولوية تطوير عملية 2 مع الشاهد الزمني]</li>
        <li>[أولوية تطوير عملية 3 مع الشاهد الزمني]</li>
        <li>[أولوية تطوير عملية 4 مع الشاهد الزمني]</li>
    </ul>
</div>

<h2>الخلاصة النهائية</h2>
<div class="exec-box">
    <p>العلامة الحالية: [ ] من 90 ([ ]%). [فقرة ختامية مركزة تلخص مستوى الأداء العام والخطوة التطويرية الأهم لرفع العلامة مستقبلاً].</p>
</div>

---
2. ويجب أن يحتوي حقل `summarizedHtml` على كود HTML الكامل لـ "التقرير المختصر" مطابقاً تماماً لهذا الهيكل الملون:

<div class="header-banner">
    <h1>نموذج تقييم أداء مقدم/ة برنامج (تقرير مختصر)</h1>
    <div class="subtitle">[اسم المقدم/ة] - [البرنامج / التاريخ]</div>
</div>

<table class="info-table">
    <tr><td>اسم المقدم/ة</td><td>[اسم المقدم/ة]</td></tr>
    <tr><td>التاريخ</td><td>[التاريخ]</td></tr>
    <tr><td>البرنامج / نوع الحلقة</td><td>[البرنامج / نوع الحلقة]</td></tr>
    <tr><td>معيار التقييم</td><td>{EDITORIAL_TYPE_LABEL}</td></tr>
    <tr><td>الضيوف والمشاركون</td><td>[الضيوف والمشاركون]</td></tr>
    <tr><td>المواد المسجلة</td><td>[المواد المسجلة]</td></tr>
    <tr><td>مدة الحلقة</td><td>[مدة الحلقة]</td></tr>
</table>

<h2>الخلاصة التنفيذية</h2>
<div class="exec-box">
    <p>[الخلاصة التنفيذية]</p>
</div>

<h2>النتيجة الإجمالية</h2>
<table class="summary-strip">
    <tr>
        <td style="width: 25%;"><span class="label">العلامة الإجمالية</span><span class="val">[ ] من 90</span></td>
        <td style="width: 25%;"><span class="label">النسبة المئوية</span><span class="val">[ ]%</span></td>
        <td style="width: 50%;" colspan="2"><span class="label">التوصيف التنفيذي المختصر</span><span class="val" style="font-size: 11.5pt;">[التوصيف المختصر]</span></td>
    </tr>
</table>

<h2>العلامات الأساسية</h2>
<table>
    <tr><th style="width: 25%;">المعيار</th><th style="width: 15%;">العلامة</th><th style="width: 60%;">ملاحظة مختصرة</th></tr>
    <tr><td><b>إدارة الحلقة</b></td><td class="score-cell">[ ] من 20</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td><b>التعامل مع الضيف</b></td><td class="score-cell">[ ] من 20</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td><b>جودة الأسئلة</b></td><td class="score-cell">[ ] من 20</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td><b>المتابعة والتعقيب</b></td><td class="score-cell">[ ] من 15</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td><b>الحضور والأداء</b></td><td class="score-cell">[ ] من 10</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td><b>ضبط الوقت</b></td><td class="score-cell">[ ] من 5</td><td>[ملاحظة مختصرة]</td></tr>
    <tr style="font-weight: bold; background-color: #DBEAFE;">
        <td><b>المجموع الكلي</b></td><td class="score-cell" style="font-size: 13pt;">[ ] من 90</td><td><b>[خلاصة عامة]</b></td>
    </tr>
</table>

<h2>نقاط القوة الرئيسية</h2>
<div class="strengths-box">
    <ul>
        <li>[نقطة قوة 1]</li>
        <li>[نقطة قوة 2]</li>
        <li>[نقطة قوة 3]</li>
    </ul>
</div>

<h2>أولويات التطوير</h2>
<div class="priorities-box">
    <ul>
        <li>[أولوية تطوير 1 - يُمنع إدراج اللهجة اللبنانية المحكية كعيب]</li>
        <li>[أولوية تطوير 2]</li>
        <li>[أولوية تطوير 3]</li>
    </ul>
</div>
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

        res = requests.post(url, json=payload, timeout=15)
        if res.status_code != 200 and use_html:
            clean_text = re.sub(r'<[^>]+>', '', message)[:4000]
            payload["text"] = clean_text
            payload.pop("parse_mode", None)
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
        except Exception as e:
            if attempt == 2:
                print(f"⚠️ تعذر إرسال الملف إلى Telegram: {e}", flush=True)
            time.sleep(3)
    return False


def send_direct_smtp_email(subject, html_body, attachment_paths=None):
    smtp_user = os.environ.get("GMAIL_USER", "").strip()
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipients_raw = os.environ.get("ALERT_EMAILS", os.environ.get("NOTIFY_EMAIL", smtp_user)).strip()
    if not smtp_user or not smtp_pass or not recipients_raw:
        return False

    recipients = [r.strip() for r in re.split(r"[,;]", recipients_raw) if r.strip()]
    if smtp_user and smtp_user not in recipients:
        recipients.insert(0, smtp_user)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(smtp_user, smtp_pass)
            for rec in recipients:
                msg = MIMEMultipart()
                msg["From"] = smtp_user
                msg["To"] = rec
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
                server.sendmail(smtp_user, [rec], msg.as_string())
        print(f"✅ تم إرسال الإيميل المباشر عبر SMTP فردياً إلى: {recipients}", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ ملاحظة SMTP المباشر: {e}", flush=True)
        return False


def save_bulletproof_backups(main_html_str, sum_html_str, summary_dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    main_path = os.path.join(BACKUP_DIR, f"01_Main_Detailed_Report_{CLEAN_NAME}.html")
    sum_path = os.path.join(BACKUP_DIR, f"02_Summarized_Report_{CLEAN_NAME}.html")
    json_path = os.path.join(BACKUP_DIR, "_run_summary.json")

    if main_html_str:
        with open(main_path, "w", encoding="utf-8") as f1:
            f1.write(main_html_str)
    if sum_html_str:
        with open(sum_path, "w", encoding="utf-8") as f2:
            f2.write(sum_html_str)
    if summary_dict:
        with open(json_path, "w", encoding="utf-8") as f3:
            json.dump(summary_dict, f3, ensure_ascii=False, indent=2)

    if main_html_str and sum_html_str:
        cost_val = float(summary_dict.get("process_cost_usd", summary_dict.get("processCost", 0.0)))
        cap_base = f"📎 نسخة احتياطية فورية ({CLEAN_NAME}) | {EDITORIAL_TYPE_LABEL} | التكلفة: ${cost_val:.4f}"
        send_telegram_document(main_path, caption=cap_base + " - التقرير التفصيلي (90 نقطة)")
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

    send_telegram(f"🔔 تحديث معالجة ({CLEAN_NAME}):\n⚙️ {MODE_TITLE}\n{msg[:300]}", use_html=False)

    if LOG_FILE_ID:
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
    cost = (in_tokens * in_rate) + (total_out * out_rate)
    return max(cost, 0.0001)


def estimate_pre_flight_cost(model_name, in_tokens, thinking_budget, expected_report_tokens=9500):
    in_rate, out_rate = get_model_rates(model_name, in_tokens)
    estimated_thinking = int(thinking_budget * 0.45) if thinking_budget > 0 else 0
    estimated_total_out = expected_report_tokens + estimated_thinking
    est_cost = (in_tokens * in_rate) + (estimated_total_out * out_rate)
    return round(est_cost, 4)


def probe_key_can_generate_on_model(api_key, model_name):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "ok"}]}],
        "generationConfig": {"maxOutputTokens": 1}
    }
    try:
        r = session.post(url, json=payload, timeout=12)
        if r.status_code == 200:
            return True, "OK"
        elif r.status_code == 429:
            err_msg = r.json().get("error", {}).get("message", "")
            match = re.search(r'Please retry in ([a-zA-Z0-9.]+)', err_msg)
            wait_t = match.group(1).split('.')[0] if match else "حصة يومية مستنفدة"
            return False, f"429 مستنفد ({wait_t})"
        else:
            return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:40]


def get_prioritized_keys():
    def is_key_alive(k_str):
        try:
            r = session.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={k_str}", timeout=12)
            return r.status_code == 200
        except Exception:
            return False

    valid_free = []
    for idx, fk in enumerate(FREE_API_KEYS, start=1):
        if is_key_alive(fk):
            valid_free.append((idx, f"FREE (Key #{idx})", fk, False))

    valid_paid = None
    if PAID_API_KEY and is_key_alive(PAID_API_KEY):
        p_idx = len(FREE_API_KEYS) + 1
        valid_paid = (p_idx, f"PAID (Key #{p_idx})", PAID_API_KEY, True)

    if FORCE_FREE:
        if not valid_free:
            raise RuntimeError(
                "رفض مسبق سريع (Pre-Flight Check): المجلد مضبوط على الوضع المجاني الإجباري (_free) "
                "ولكن لا يوجد أي مفتاح مجاني صالح في الإعدادات. غير اسم المجلد إلى _paid لتشغيله."
            )
        log_step(f"🆓 وضع المجاني الإجباري (_free): تم التحقق من {len(valid_free)} مفاتيح مجانية صالحة (منع استخدام المفتاح المدفوع).")
        return valid_free

    ordered = list(valid_free)
    if valid_paid:
        ordered.append(valid_paid)

    if not ordered:
        raise RuntimeError("لا توجد أي مفاتيح Gemini صالحة (لا مجانية ولا مدفوعة) في GitHub Secrets.")

    log_step(f"🔑 المفاتيح الفعالة الجاهزة: {len(valid_free)} مجاني + {1 if valid_paid else 0} مدفوع.")
    return ordered


def update_drive_folder(new_name, description_text=""):
    global ORIGINAL_SOURCE_URL
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": "application/json"}
    payload = {"name": new_name}
    if description_text:
        if not ORIGINAL_SOURCE_URL:
            try:
                cur_meta = get_drive_folder_metadata()
                ORIGINAL_SOURCE_URL = extract_url_from_text(cur_meta.get("description", "") or "")
            except Exception:
                pass
        if ORIGINAL_SOURCE_URL and ORIGINAL_SOURCE_URL not in description_text:
            description_text = f"🔗 رابط المصدر: {ORIGINAL_SOURCE_URL}\n\n" + description_text
        payload["description"] = description_text
    try:
        r = session.patch(f"https://www.googleapis.com/drive/v3/files/{FOLDER_ID}", headers=headers, json=payload, timeout=25)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"⚠️ تحذير تحديث المجلد على Drive: {e}", flush=True)
        return False


def upload_report_to_drive(filename, content_str, mime_type="text/html"):
    headers_list = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    q = f"'{FOLDER_ID}' in parents and name = '{filename}' and trashed = false"
    existing_ids = []
    try:
        r_list = session.get("https://www.googleapis.com/drive/v3/files", headers=headers_list, params={"q": q, "fields": "files(id)"}, timeout=20)
        if r_list.status_code == 200:
            existing_ids = [f["id"] for f in r_list.json().get("files", [])]
    except Exception:
        pass

    try:
        if len(existing_ids) > 1:
            for old_id in existing_ids[1:]:
                try:
                    session.delete(f"https://www.googleapis.com/drive/v3/files/{old_id}", headers=headers_list, timeout=15)
                except Exception:
                    pass

        if existing_ids:
            up_url = f"https://www.googleapis.com/upload/drive/v3/files/{existing_ids[0]}?uploadType=media"
            r = session.patch(
                up_url,
                headers={"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"{mime_type}; charset=UTF-8"},
                data=content_str.encode("utf-8"),
                timeout=45
            )
        else:
            metadata = {"name": filename, "parents": [FOLDER_ID], "mimeType": mime_type}
            boundary = "===boundary_report==="
            body = (
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\n"
                f"Content-Type: {mime_type}; charset=UTF-8\r\n\r\n"
                f"{content_str}\r\n"
                f"--{boundary}--"
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {DRIVE_TOKEN}", "Content-Type": f"multipart/related; boundary={boundary}"}
            r = session.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart", headers=headers, data=body, timeout=45)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"⚠️ تحذير رفع التقرير {filename} إلى Drive: {e}", flush=True)
        return False


def get_drive_folder_metadata():
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    r = session.get(
        f"https://www.googleapis.com/drive/v3/files/{FOLDER_ID}",
        headers=headers,
        params={"fields": "id,name,description", "supportsAllDrives": "true"},
        timeout=25
    )
    if r.status_code == 200:
        return r.json()
    return {}


def list_drive_folder_contents():
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    q = f"'{FOLDER_ID}' in parents and trashed = false"
    res = session.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={"q": q, "fields": "files(id, name, mimeType, size)"},
        timeout=30
    )
    res.raise_for_status()
    return res.json().get("files", [])


def extract_url_from_text(text):
    if not text:
        return None
    for m in re.finditer(r'(https?://[^\s<>"\'\r\n]+)', text):
        candidate = m.group(1).strip().rstrip('.,;)]}')
        if "drive.google.com/drive/folders/" not in candidate:
            return candidate
    return None


def find_video_url_in_folder(all_files, folder_desc=""):
    """
    قراءة مضمونة 100% للرابط من:
    1. وصف المجلد (Folder Description) مباشرة.
    2. ملف _source_link.txt الذي ينشئه Code.gs تلقائياً من الوصف.
    3. أي مستند Google Doc أو ملف نصي داخل المجلد.
    """
    global ORIGINAL_SOURCE_URL
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}

    if folder_desc:
        url = extract_url_from_text(folder_desc)
        if url:
            ORIGINAL_SOURCE_URL = url
            return url, "وصف المجلد (Folder Description)"

    try:
        meta = get_drive_folder_metadata()
        desc_live = meta.get("description", "") or ""
        url = extract_url_from_text(desc_live)
        if url:
            ORIGINAL_SOURCE_URL = url
            return url, "وصف المجلد (Folder Description)"
    except Exception:
        pass

    for f in all_files:
        fname = f.get("name", "")
        if fname.startswith("00_") or fname == "_run_summary.json" or fname.endswith(".html") or fname.endswith(".json"):
            continue
        mime = f.get("mimeType", "")
        try:
            if mime == "application/vnd.google-apps.document":
                r = session.get(
                    f"https://www.googleapis.com/drive/v3/files/{f['id']}/export",
                    headers=headers,
                    params={"mimeType": "text/plain"},
                    timeout=20
                )
                if r.status_code == 200:
                    url = extract_url_from_text(r.text)
                    if url:
                        ORIGINAL_SOURCE_URL = url
                        return url, f"مستند Google Doc ({fname})"
            elif fname == "_source_link.txt" or mime.startswith("text/") or fname.lower().endswith((".txt", ".url", ".webloc", ".md")):
                r = session.get(
                    f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media",
                    headers=headers,
                    timeout=20
                )
                if r.status_code == 200:
                    url = extract_url_from_text(r.text)
                    if url:
                        ORIGINAL_SOURCE_URL = url
                        src_label = "وصف المجلد (_source_link.txt)" if fname == "_source_link.txt" else f"الملف النصي ({fname})"
                        return url, src_label
        except Exception:
            pass

    return None, None


def upload_binary_file_to_drive_folder(local_path, drive_filename, mime_type="video/mp4"):
    filesize = os.path.getsize(local_path)
    init_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
    headers = {
        "Authorization": f"Bearer {DRIVE_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": mime_type,
        "X-Upload-Content-Length": str(filesize)
    }
    metadata = {"name": drive_filename, "parents": [FOLDER_ID]}
    r = session.post(init_url, headers=headers, json=metadata, timeout=30)
    r.raise_for_status()
    upload_url = r.headers["Location"]

    chunk_size = 16 * 1024 * 1024
    offset = 0
    with open(local_path, "rb") as f:
        while offset < filesize:
            chunk = f.read(chunk_size)
            chunk_len = len(chunk)
            end_byte = offset + chunk_len - 1
            chunk_headers = {
                "Content-Length": str(chunk_len),
                "Content-Range": f"bytes {offset}-{end_byte}/{filesize}"
            }
            up_res = session.put(upload_url, headers=chunk_headers, data=chunk, timeout=180)
            if up_res.status_code not in (200, 201, 308):
                up_res.raise_for_status()
            offset += chunk_len


def download_drive_file(file_id, local_path):
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    with session.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)


def is_youtube_url(url):
    u = str(url or "").lower()
    return ("youtube.com/watch" in u) or ("youtu.be/" in u) or ("youtube.com/live/" in u) or ("youtube.com/shorts/" in u)


def download_video_from_external_url(url, local_path):
    # 1. إذا كان الرابط لملف على Google Drive
    gdrive_match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]{20,})', url)
    if gdrive_match and "drive.google.com" in url:
        file_id = gdrive_match.group(1)
        log_step("☁️ الرابط هو ملف Google Drive - جاري السحب المباشر...")
        try:
            download_drive_file(file_id, local_path)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
                return local_path
        except Exception as e:
            log_step(f"⚠️ تعذر السحب المباشر عبر API ({str(e)[:40]})، جاري المحاولة عبر yt-dlp...")

    # 2. إذا كان رابط ميديا مباشر (MP4 / MOV / MKV / TS)
    is_direct_media = url.lower().split("?")[0].endswith((".mp4", ".mov", ".mkv", ".mp3", ".m4a", ".ts"))
    if is_direct_media:
        try:
            log_step("🌐 جاري التحميل المباشر لملف الميديا من الرابط...")
            with session.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "").lower()
                if "text/html" not in ctype:
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    if os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
                        return local_path
        except Exception as e:
            log_step(f"⚠️ التحويل إلى الفحص المباشر عبر FFprobe/yt-dlp ({str(e)[:40]})...")

    # 3. فحص ما إذا كان الرابط يقبل التقطيع المباشر عبر FFmpeg (لغير يوتيوب)
    if not is_youtube_url(url):
        try:
            probe_chk = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", url],
                capture_output=True, text=True, timeout=25
            )
            if probe_chk.returncode == 0 and float(probe_chk.stdout.strip() or 0) > 0:
                log_step("⚡ الرابط يدعم البث/التقطيع المباشر عبر FFmpeg!")
                return url
        except Exception:
            pass

    # 4. السحب عبر yt-dlp مع محاكاة تطبيقات الموبايل لتخطي حظر يوتيوب
    log_step("⚙️ تجهيز محرك السحب السحابي (yt-dlp) وسحب الحلقة من الرابط...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=False)
    if os.path.exists(local_path):
        os.remove(local_path)

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--no-check-certificates",
        "--extractor-args", "youtube:player_client=ios,android,mweb,tv",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", local_path,
        url
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
        return local_path

    # 5. إذا كان الرابط هو رابط يوتيوب وحظر يوتيوب عنوان IP خادم GitHub، نمرر الرابط مباشرة إلى خوادم Gemini!
    if is_youtube_url(url):
        log_step("⚡ يوتيوب قيّد التحميل المباشر على IP الخادم — سيتم تمرير رابط YouTube مباشرة إلى محرك Gemini لتحليله من خوادم جوجل!")
        return f"DIRECT_YOUTUBE_URI::{url}"

    err_tail = (res.stderr or res.stdout or "خطأ غير معروف")[-300:]
    raise RuntimeError(f"فشل سحب الفيديو من الرابط ({url}):\n{err_tail}")


def get_duration_seconds(filepath_or_url):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath_or_url]
    out = subprocess.check_output(cmd).decode("utf-8").strip()
    return float(out)


def format_hhmmss(seconds):
    s = int(round(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def prepare_media_segments(drive_files, external_url=None, url_source=None):
    local_raw = "raw_episode.mp4"
    if external_url:
        log_step(f"🔗 تم التقاط رابط الحلقة من {url_source}:\n{external_url}")
        media_input = download_video_from_external_url(external_url, local_raw)

        if str(media_input).startswith("DIRECT_YOUTUBE_URI::"):
            return [media_input], "كامل مدة فيديو يوتيوب"

        if media_input == local_raw and os.path.exists(local_raw):
            size_mb = os.path.getsize(local_raw) / (1024 * 1024)
            log_step(f"✅ اكتمل سحب الفيديو من الرابط بنجاح ({size_mb:.1f} MB)!")
            if "_NOSAVE" not in UPPER_NAME:
                try:
                    drive_mp4_name = f"{CLEAN_NAME}.mp4"
                    log_step(f"☁️ جاري حفظ نسخة من الفيديو ({drive_mp4_name} - {size_mb:.1f} MB) داخل مجلد Google Drive...")
                    upload_binary_file_to_drive_folder(local_raw, drive_mp4_name, mime_type="video/mp4")
                    log_step("✅ تم حفظ ملف الـ MP4 داخل مجلد Google Drive بنجاح!")
                except Exception as up_e:
                    log_step(f"⚠️ تعذر حفظ نسخة الـ MP4 في الدرايف ({str(up_e)[:50]})، جاري متابعة التقييم...")

        total_sec = get_duration_seconds(media_input)
        exact_duration = format_hhmmss(total_sec)
        log_step(f"⏱️ المدة الفعلية للحلقة: {exact_duration}")

        if IS_LOW_MODE:
            log_step("✂️ تطبيق إعدادات (_LOW): (صورة كل 5 ثوان - دقة 480p) + استخراج الصوت...")
            subprocess.run(["ffmpeg", "-y", "-i", media_input, "-vn", "-c:a", "libmp3lame", "-ac", "1", "-ar", "24000", "-b:a", "48k", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Audio_part%02d.mp3"], check=True)
            subprocess.run(["ffmpeg", "-y", "-i", media_input, "-an", "-c:v", "libx264", "-preset", "faster", "-crf", "32", "-g", "5", "-maxrate", "100k", "-bufsize", "200k", "-vf", "scale=-2:480,fps=0.2", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Visuals_part%02d.mp4"], check=True)
        else:
            log_step("✂️ جاري تشغيل FFmpeg لاستخراج وتقسيم الصوت (MP3) كل 25 دقيقة...")
            subprocess.run(["ffmpeg", "-y", "-i", media_input, "-vn", "-c:a", "libmp3lame", "-ac", "1", "-ar", "24000", "-b:a", "64k", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Audio_part%02d.mp3"], check=True)
            log_step("🎞️ جاري تشغيل FFmpeg لاستخراج وتقسيم الصورة (فيديو خفيف 720p - 1fps) كل 25 دقيقة...")
            subprocess.run(["ffmpeg", "-y", "-i", media_input, "-an", "-c:v", "libx264", "-preset", "faster", "-crf", "30", "-maxrate", "150k", "-bufsize", "300k", "-vf", "scale=-2:720,fps=1", "-f", "segment", "-segment_time", "1500", "-reset_timestamps", "1", "Visuals_part%02d.mp4"], check=True)

        segments = sorted(glob.glob("Audio_part*.mp3")) + sorted(glob.glob("Visuals_part*.mp4"))
        return segments, exact_duration

    if len(drive_files) == 1 and drive_files[0]["mimeType"].startswith("video/"):
        raw_file = drive_files[0]
        size_mb = int(raw_file.get("size", 0)) / (1024 * 1024)
        log_step(f"📥 سحب ملف الفيديو الخام ({raw_file['name']} - {size_mb:.1f} MB)...")
        download_drive_file(raw_file["id"], local_raw)
        total_sec = get_duration_seconds(local_raw)
        exact_duration = format_hhmmss(total_sec)
        log_step(f"⏱️ المدة الفعلية للحلقة: {exact_duration}")

        if IS_LOW_MODE:
            log_step("✂️ تطبيق إعدادات (_LOW): (صورة كل 5 ثوان - دقة 480p) + استخراج الصوت...")
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
    if str(filepath).startswith("DIRECT_YOUTUBE_URI::"):
        yt_url = filepath.split("DIRECT_YOUTUBE_URI::", 1)[1]
        return yt_url, "video/mp4"

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
# المسار الأول: النسخة الاحترافية المتدرجة (تختار النموذج وتفحص الكوتا قبل رفع أي ملف!)
# ==============================================================================
def evaluate_standard_pro_mode(segments, exact_duration, total_segments_count):
    all_candidate_models = [
        ("gemini-3.1-pro-preview", 24576, "gemini-3.1-pro-preview (Max Thinking 24576)", True),
        ("gemini-3.1-pro-preview", 8192,  "gemini-3.1-pro-preview (Med Thinking 8192)",  False),
        ("gemini-3.8-flash",       24576, "gemini-3.8-flash (High Thinking 24576)",      False),
        ("gemini-3.8-flash",       8192,  "gemini-3.8-flash (Med Thinking 8192)",       False),
        ("gemini-3.8-flash",       0,     "gemini-3.8-flash (Standard Fast)",            False),
        ("gemini-3.7-flash",       8192,  "gemini-3.7-flash (Med Thinking 8192)",       False),
        ("gemini-3.6-flash",       0,     "gemini-3.6-flash (Standard Fast)",            False),
    ]
    lean_first_models = [
        ("gemini-3.8-flash",       8192,  "gemini-3.8-flash (Med Thinking 8192)",       False),
        ("gemini-3.8-flash",       0,     "gemini-3.8-flash (Standard Fast)",            False),
        ("gemini-3.7-flash",       0,     "gemini-3.7-flash (Standard Fast)",            False),
        ("gemini-3.6-flash",       0,     "gemini-3.6-flash (Standard Fast)",            False),
    ]
    base_models = lean_first_models if IS_PAID_MIN_COST else all_candidate_models

    audio_segs = sorted(glob.glob("Audio_part*.mp3"))
    seg_timeline_info = []
    accum_sec = 0.0
    for idx, aseg in enumerate(audio_segs):
        dur = get_duration_seconds(aseg)
        start_str = format_hhmmss(accum_sec)
        end_str = format_hhmmss(accum_sec + dur)
        seg_timeline_info.append(f"- الجزء {idx:02d} ({aseg}): [{start_str}] -> [{end_str}]")
        accum_sec += dur
    if not seg_timeline_info:
        accum_sec = 3600.0
        seg_timeline_info.append(f"- الحلقة كاملة متصلة من البداية حتى النهاية ({exact_duration})")
    timeline_map_str = "\n".join(seg_timeline_info)

    est_in_tokens = int((accum_sec * 295) + 4800)
    remaining_monthly = max(0.0, round(MAX_MONTHLY_BUDGET - get_monthly_spend(), 4))
    effective_cap = min(EPISODE_MAX_COST, remaining_monthly)

    eligible_models = []
    for m_name, m_budget, m_label, m_high in base_models:
        if FORCE_FREE:
            if "pro" not in m_name.lower():
                eligible_models.append((m_name, m_budget, m_label, m_high))
        else:
            est_c = estimate_pre_flight_cost(m_name, est_in_tokens, m_budget)
            if est_c <= effective_cap:
                eligible_models.append((m_name, m_budget, m_label, m_high))
            else:
                log_step(f"💸 استبعاد مسبق لـ {m_label} قبل الرفع: تكلفته المقدرة (${est_c:.3f}) أعلى من سقف الحلقة (${effective_cap:.2f})")

    if not eligible_models:
        log_step("🔄 جميع نماذج الدفعة الواحدة تتجاوز السقف المحدد — التحول التلقائي الذكي إلى مسار التجزئة الاقتصادية (_LOW)!")
        return evaluate_low_free_mode(segments, exact_duration, total_segments_count)

    log_step(f"🎯 النموذج المرشح الأول المطابق لسقف الميزانية: {eligible_models[0][2]}")

    system_prompt = (
        f"{SKILL_INSTRUCTIONS_AND_RUBRIC}\n\n"
        "================================================================================\n"
        "رابعاً: خريطة التوقيت المتصل للأجزاء ومتطلبات إخراج JSON\n"
        "================================================================================\n"
        f"1. خريطة التوقيت المتصل للأجزاء المرفقة (المدة الإجمالية الدقيقة للحلقة {exact_duration}):\n{timeline_map_str}\n"
        f"2. قاعدة التوقيت المطلق المتصل: اجمع توقيت أي لقطة أو اقتباس داخل الجزء مع وقت بداية ذلك الجزء الموضح في الخريطة أعلاه، لتكون كافة التوقيتات في التقرير متصلة من بداية الحلقة (00:00:00) حتى نهايتها ({exact_duration}).\n"
        "3. التوثيق الحرفي لآخر كلام في الحلقة (lastSpokenWords): يجب عليك الاستماع إلى آخر دقيقة في آخر ملف صوتي مرفق، وتدوين النص الحرفي (Verbatim) لآخر جملتين أو ثلاث جمل قالها المقدم أو الضيف قبل انتهاء الحلقة مباشرة مع توقيتها المتصل.\n"
        "4. أرجع كائن JSON حصراً بالحقول التالية:\n"
        "   - isFullDurationAnalyzed: (boolean) يجب أن يكون true حصراً بعد تحليل كامل المدة.\n"
        "   - analyzedAllSegments: (boolean) يجب أن يكون true حصراً إذا تم تحليل كافة الأجزاء حتى الثانية الأخيرة.\n"
        "   - lastSpokenWords: (string) النص الحرفي لآخر كلام قيل في ختام الحلقة مع التوقيت المتصل.\n"
        "   - mainDetailedHtml: (string) كود HTML الكامل والمفصل للتقرير الرئيسي شاملاً الجداول الفرعية الستة (23 معياراً فرعياً) والاقتباسات الحرفية والرصد البصري للغة الجسد وجدول الأمثلة الموثقة (10 أمثلة على الأقل).\n"
        "   - summarizedHtml: (string) كود HTML الكامل للتقرير المختصر شاملاً جدول البيانات والنتيجة الإجمالية وجدول المعايير الستة الأساسية."
    )

    prioritized_keys = get_prioritized_keys()
    errors = []

    for key_idx, key_label, api_key, is_paid_key in prioritized_keys:
        if is_paid_key:
            current_monthly_spend = get_monthly_spend()
            if current_monthly_spend >= MAX_MONTHLY_BUDGET:
                ceil_msg = f"تم إيقاف العملية: وصل الإنفاق الشهري إلى الحد الأقصى (${current_monthly_spend:.4f} / ${MAX_MONTHLY_BUDGET:.2f})."
                log_step(f"🛑 {ceil_msg}")
                errors.append(f"• {key_label}: {ceil_msg}")
                raise RuntimeError("\n".join(errors))

        key_models = eligible_models if is_paid_key else [m for m in eligible_models if "pro" not in m[0].lower()]
        if not key_models:
            key_models = eligible_models

        probe_ok = False
        active_key_models = []
        for m_tuple in key_models:
            ok, reason = probe_key_can_generate_on_model(api_key, m_tuple[0])
            if ok:
                probe_ok = True
                active_key_models.append(m_tuple)
            else:
                if not is_paid_key:
                    log_step(f"⚡ تخطي {key_label} فوراً بدون رفع ملفات (نتيجة الفحص اللحظي على {m_tuple[0]}: {reason})")
                    errors.append(f"• {key_label} [{m_tuple[0]}]: {reason}")
                    break

        if not probe_ok:
            continue

        log_step(f"✅ المفتاح {key_label} اجتاز الفحص اللحظي بنجاح! جاري تجهيز/رفع المقاطع الآن...")

        try:
            prompt_parts = []
            for seg in segments:
                log_step(f"☁️ ربط/رفع الجزء ({seg}) إلى Gemini باستخدام {key_label}...")
                uri, mime = upload_to_gemini_file_api(seg, api_key)
                prompt_parts.append({"text": f"الملف المرفق التالي هو: {seg}"})
                prompt_parts.append({"fileData": {"mimeType": mime, "fileUri": uri}})
        except Exception as up_err:
            log_step(f"⚠️ فشل رفع الملفات عبر {key_label} - الانتقال للمفتاح التالي...")
            errors.append(f"• {key_label}: فشل رفع الملفات ({str(up_err)[:80]})")
            continue

        user_instruction = (
            f"قم بتحليل وتقييم حلقة ({CLEAN_NAME}) بالكامل من 00:00:00 إلى {exact_duration} "
            f"وفق [{EDITORIAL_TYPE_LABEL}] والسلم المعتمد (90 نقطة - 6 معايير أساسية و23 معياراً فرعياً) والقوالب الملونة المعتمدة في التعليمات، "
            "مع الالتزام التام بقاعدة اللهجة اللبنانية المحكية، وإيراد الاقتباسات الحرفية والشواهد البصرية للغة الجسد بالتوقيتات الدقيقة."
        )
        full_contents = [{"role": "user", "parts": prompt_parts + [{"text": user_instruction}]}]

        for model_name, budget, label, is_highest in active_key_models:
            log_step(f"🧠 بدء التحليل العميق لجميع الأجزاء عبر النموذج: {label} ({key_label})...")
            gen_cfg = {
                "temperature": 0.0,
                "maxOutputTokens": 65536,
                "responseMimeType": "application/json"
            }
            if budget > 0:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}

            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": full_contents,
                "generationConfig": gen_cfg
            }
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

            try:
                res = session.post(url, json=payload, timeout=600 if is_paid_key else 210)
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
                        log_step(f"🆓 اكتملت العملية مجاناً ($0.0000) عبر {key_label}!")

                    banner = f'<div class="thinking-note">المنظومة: {EDITORIAL_TYPE_LABEL} | النموذج: {label} ({key_label}) | تكلفة العملية: ${process_cost:.4f}</div>'
                    parsed["thinkingBanner"] = banner
                    parsed["costInfo"] = {
                        "keyLabel": key_label,
                        "modelUsed": label,
                        "isPaid": is_paid_key,
                        "processCost": round(process_cost, 4),
                        "totalMonthly": round(total_monthly, 4)
                    }
                    return parsed
                elif res.status_code == 429:
                    err_msg = res.json().get("error", {}).get("message", "")
                    match = re.search(r'Please retry in ([a-zA-Z0-9.]+)', err_msg)
                    wait_time = match.group(1).split('.')[0] if match else "مؤقت"
                    clean_err = f"المفتاح مستنفد لـ {len(segments)} أجزاء مجتمعة ({wait_time})"
                    log_step(f"⚡ {clean_err} على {key_label} - انتقال فوري بدون انتظار...")
                    errors.append(f"• {key_label} [{label}]: {clean_err}")
                    if not is_paid_key:
                        break
                    continue
                elif res.status_code == 503:
                    log_step(f"⚡ ضغط سيرفرات جوجل (503) على {label} ({key_label}) - انتقال فوري...")
                    errors.append(f"• {key_label} [{label}]: ضغط سيرفرات جوجل (503)")
                    continue
                else:
                    log_step(f"⚠️ استجابة HTTP {res.status_code} على {label} ({key_label}) - انتقال للتالي...")
                    errors.append(f"• {key_label} [{label}]: HTTP {res.status_code}")
                    continue
            except Exception as req_err:
                log_step(f"⚠️ خطأ اتصال على {label} ({key_label}) - انتقال للنموذج التالي...")
                errors.append(f"• {key_label} [{label}]: خطأ شبكة ({str(req_err)[:50]})")
                if not is_paid_key:
                    break
                continue

    raise RuntimeError("\n".join(errors))


# ==============================================================================
# المسار الثاني: نسخة (_LOW) - تحليل كل مقطع على حدة مع الفحص اللحظي قبل الرفع
# ==============================================================================
def call_gemini_low_mode_step(system_prompt, user_parts_builder, models_list, step_title, expect_json=True, current_ep_spent=0.0):
    prioritized_keys = get_prioritized_keys()
    errors = []

    for key_idx, key_label, api_key, is_paid_key in prioritized_keys:
        if is_paid_key:
            current_monthly_spend = get_monthly_spend()
            if current_monthly_spend >= MAX_MONTHLY_BUDGET:
                ceil_msg = f"وصل الإنفاق الشهري للحد الأقصى (${current_monthly_spend:.4f} / ${MAX_MONTHLY_BUDGET:.2f})."
                log_step(f"🛑 {ceil_msg}")
                raise RuntimeError(ceil_msg)
            if current_ep_spent >= EPISODE_MAX_COST:
                ep_ceil_msg = f"وصلت التكلفة التراكمية (${current_ep_spent:.4f}) إلى سقف الحلقة (${EPISODE_MAX_COST:.2f})."
                log_step(f"🛑 {ep_ceil_msg}")
                raise RuntimeError(ep_ceil_msg)

        if not is_paid_key:
            ok, reason = probe_key_can_generate_on_model(api_key, models_list[0][0])
            if not ok:
                log_step(f"⚡ تخطي {key_label} فوراً بدون رفع ملفات ({reason})")
                errors.append(f"• {key_label}: {reason}")
                continue

        try:
            prompt_parts = user_parts_builder(api_key, key_label)
        except Exception as up_err:
            errors.append(f"• {key_label} ({step_title}): فشل رفع الملفات ({str(up_err)[:50]})")
            continue

        for model_name, budget, label, is_highest in models_list:
            max_attempts = 3 if is_paid_key else 1
            for attempt in range(1, max_attempts + 1):
                log_step(f"🧠 [{step_title}] عبر {label} ({key_label} - محاولة {attempt}/{max_attempts})...")
                gen_cfg = {"temperature": 0.0, "maxOutputTokens": 65536}
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
                    res = session.post(url, json=payload, timeout=400 if is_paid_key else 180)
                    if res.status_code == 200:
                        data = res.json()
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        step_cost = 0.0
                        if is_paid_key:
                            usage = data.get("usageMetadata", {})
                            step_cost = calculate_call_cost(model_name, usage)
                            add_monthly_spend(step_cost)
                        return text, label, key_label, is_paid_key, step_cost

                    elif res.status_code in (429, 503):
                        clean_err = f"HTTP {res.status_code}"
                        if not is_paid_key:
                            log_step(f"⚡ [{step_title}] المفتاح المجاني ({key_label}) مشغول ({res.status_code}) — انتقال فوري!")
                            errors.append(f"• {key_label} [{label}]: {clean_err}")
                            break
                        if attempt < max_attempts:
                            wait_sec = 25 * attempt
                            time.sleep(wait_sec)
                            continue
                        errors.append(f"• {key_label} [{label}]: {clean_err}")
                        break
                    else:
                        errors.append(f"• {key_label} [{label}]: HTTP {res.status_code}")
                        break
                except Exception as req_err:
                    errors.append(f"• {key_label} [{label}]: {str(req_err)[:50]}")
                    if not is_paid_key:
                        break
                    if attempt < max_attempts:
                        time.sleep(15)
                        continue
                    break

    raise RuntimeError("\n".join(errors))


def evaluate_low_free_mode(segments, exact_duration, total_segments_count):
    if len(segments) == 1 and str(segments[0]).startswith("DIRECT_YOUTUBE_URI::"):
        return evaluate_standard_pro_mode(segments, exact_duration, total_segments_count)

    models = [
        ("gemini-3.8-flash", 0,    "gemini-3.8-flash (Standard Fast)", True),
        ("gemini-3.8-flash", 8192, "gemini-3.8-flash (Thinking 8192)", False),
        ("gemini-3.7-flash", 0,    "gemini-3.7-flash (Standard Fast)", False),
        ("gemini-3.6-flash", 0,    "gemini-3.6-flash (Standard Fast)", False),
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
            f"{SKILL_INSTRUCTIONS_AND_RUBRIC}\n\n"
            f"أنت الآن تحلل المقطع رقم ({idx + 1} من {num_parts}) والذي يمتد في الحلقة الأصلية من التوقيت [{p['start_str']}] إلى [{p['end_str']}].\n"
            "قواعد صارمة لهذا المقطع:\n"
            f"1. قاعدة التوقيت المتصل: أضف دائماً وقت بداية هذا المقطع ({p['start_str']}) إلى أي توقيت ترصده داخل الملف، لتكون جميع التوقيتات المدونة بين [{p['start_str']}] و [{p['end_str']}].\n"
            "2. استخرج تقريراً تحليلياً تفصيلياً وشاملاً لهذا المقطع يغطي المعايير الـ 6 الأساسية والـ 23 معياراً فرعياً، مع تدوين الاقتباسات الحرفية والرصد البصري الدقيق للغة الجسد بتوقيتاتها المتصلة، ومراعاة قاعدة اللهجة اللبنانية المحكية.\n"
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
            expect_json=False,
            current_ep_spent=total_process_cost
        )
        if is_paid:
            any_paid_used = True
            total_process_cost += step_cost
        part_summaries.append(f"=== تحليل المقطع {idx + 1} (من {p['start_str']} إلى {p['end_str']}) ===\n{part_text}")

    log_step("📊 دمج تحليلات جميع المقاطع وإصدار التقرير النهائي الموحد (90 نقطة)...")
    combined_notes = "\n\n".join(part_summaries)

    final_sys_prompt = (
        f"{SKILL_INSTRUCTIONS_AND_RUBRIC}\n\n"
        f"أمامك التحليلات التفصيلية الكاملة لجميع مقاطع الحلقة ({CLEAN_NAME}) والبالغة مدتها الإجمالية ({exact_duration}).\n"
        "قواعد إجبارية صارمة:\n"
        f"1. قم بدمج وتوحيد هذه التحليلات في تقييم نهائي واحد متكامل وشامل للحلقة كاملة من (00:00:00) حتى ({exact_duration}) وفق المعايير الستة الأساسية والـ 23 معياراً فرعياً والقوالب الملونة المعتمدة.\n"
        "2. يمنع نهائياً كتابة عبارات مثل (في الجزء الأول أو المقطع الثاني)، بل تعامل مع الحلقة كوحدة متصلة بالتوقيتات المطلقة.\n"
        "3. استخرج النص الحرفي لآخر كلام قيل في ختام الحلقة مع توقيته المتصل وضعه في حقل lastSpokenWords.\n"
        "4. أرجع كائن JSON حصراً بالحقول التالية:\n"
        "   - isFullDurationAnalyzed: (boolean) ضعه true\n"
        "   - analyzedAllSegments: (boolean) ضعه true\n"
        "   - lastSpokenWords: (string) النص الحرفي لآخر كلام قيل في ختام الحلقة مع التوقيت المتصل.\n"
        "   - mainDetailedHtml: (string) التقرير الرئيسي المفصل والمنسق بـ HTML وفق القالب الملون المعتمد.\n"
        "   - summarizedHtml: (string) التقرير المختصر والمنسق بـ HTML وفق القالب الملون المعتمد."
    )

    def build_final_text_parts(api_key, key_label):
        return [{"text": f"فيما يلي التحليل المفصل لكافة مقاطع الحلقة من البداية للنهاية، قم بإصدار كائن JSON النهائي للتقييم:\n\n{combined_notes}"}]

    final_json_str, used_label, used_key_label, is_paid, step_cost = call_gemini_low_mode_step(
        system_prompt=final_sys_prompt,
        user_parts_builder=build_final_text_parts,
        models_list=models,
        step_title="إصدار التقرير النهائي الموحد",
        expect_json=True,
        current_ep_spent=total_process_cost
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
        f'<div class="thinking-note">المنظومة: {MODE_TITLE} | النموذج: {used_label} ({used_key_label}) | التكلفة: ${total_process_cost:.4f}</div>'
    )
    parsed["costInfo"] = {
        "keyLabel": used_key_label,
        "modelUsed": used_label,
        "isPaid": any_paid_used,
        "processCost": round(total_process_cost, 4),
        "totalMonthly": round(get_monthly_spend(), 4)
    }
    return parsed


def evaluate_with_gemini(segments, exact_duration, total_segments_count):
    if IS_LOW_MODE:
        return evaluate_low_free_mode(segments, exact_duration, total_segments_count)
    else:
        return evaluate_standard_pro_mode(segments, exact_duration, total_segments_count)


def clean_inner_html(raw_html):
    body_match = re.search(r'<body[^>]*>(.*)</body>', raw_html, flags=re.DOTALL | re.IGNORECASE)
    if body_match:
        return body_match.group(1).strip()
    raw_html = re.sub(r'<!DOCTYPE[^>]*>', '', raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r'</?html[^>]*>', '', raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r'<head>.*?</head>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r'</?body[^>]*>', '', raw_html, flags=re.IGNORECASE)
    return raw_html.strip()


def wrap_html(title, body_content):
    cleaned_body = clean_inner_html(body_content)
    return f"""<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office"
      xmlns:w="urn:schemas-microsoft-com:office:word"
      xmlns="http://www.w3.org/TR/REC-html40"
      dir="rtl" lang="ar">
<head>
<meta charset="utf-8">
<title>{title}</title>
<!--[if gte mso 9]>
<xml>
  <w:WordDocument>
    <w:View>Print</w:View>
    <w:Zoom>100</w:Zoom>
    <w:DoNotOptimizeForBrowser/>
  </w:WordDocument>
</xml>
<![endif]-->
<style>
  @page {{ size: A4; margin: 1.8cm; }}
  body {{
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
  }}
  .low-mode-banner {{
      background-color: #FFF3CD;
      color: #856404;
      border: 2px solid #FFEEBA;
      border-right: 6px solid #FFC107;
      padding: 12px 16px;
      margin-bottom: 16px;
      border-radius: 6px;
      font-size: 11pt;
  }}
  .thinking-note {{
      font-size: 9.5pt;
      color: #1E3A8A;
      background-color: #EFF6FF;
      border: 1px solid #BFDBFE;
      border-right: 4px solid #2563EB;
      padding: 7px 12px;
      margin-bottom: 16px;
      border-radius: 4px;
      text-align: right;
      font-weight: bold;
  }}
  .header-banner {{
      background-color: #1E3A8A;
      color: #FFFFFF;
      padding: 22px 24px;
      border-radius: 8px;
      border-bottom: 5px solid #F59E0B;
      margin-bottom: 22px;
      text-align: center;
  }}
  h1 {{
      text-align: center;
      font-size: 20pt;
      font-weight: bold;
      color: #1E3A8A;
      margin: 0 0 6px 0;
      page-break-after: avoid;
  }}
  .header-banner h1 {{
      color: #FFFFFF;
  }}
  .subtitle {{
      text-align: center;
      font-size: 13.5pt;
      font-weight: bold;
      color: #475569;
      margin: 0 0 14px 0;
      page-break-after: avoid;
  }}
  .header-banner .subtitle {{
      color: #E2E8F0;
      margin: 0;
  }}
  h2 {{
      font-size: 14pt;
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
  }}
  p {{
      margin-top: 6px;
      margin-bottom: 12px;
      text-align: justify;
  }}
  .exec-box {{
      background-color: #EFF6FF;
      border: 1px solid #BFDBFE;
      border-right: 6px solid #1D4ED8;
      padding: 14px 18px;
      border-radius: 6px;
      margin-bottom: 18px;
  }}
  .strengths-box {{
      background-color: #F0FDF4;
      border: 1px solid #BBF7D0;
      border-right: 6px solid #16A34A;
      padding: 14px 20px;
      border-radius: 6px;
      margin-bottom: 18px;
  }}
  .strengths-box li {{ color: #064E3B; font-weight: 500; }}
  .priorities-box {{
      background-color: #FFFBEB;
      border: 1px solid #FDE68A;
      border-right: 6px solid #D97706;
      padding: 14px 20px;
      border-radius: 6px;
      margin-bottom: 18px;
  }}
  .priorities-box li {{ color: #78350F; font-weight: 500; }}
  table {{
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 18px;
      page-break-inside: avoid;
  }}
  table, th, td {{ border: 1px solid #CBD5E1; }}
  th, td {{
      padding: 9px 12px;
      text-align: right;
      vertical-align: top;
      page-break-inside: avoid;
  }}
  tr {{ page-break-inside: avoid; page-break-after: auto; }}
  tr:nth-child(even) td {{ background-color: #F8FAFC; }}
  th {{
      background-color: #1E3A8A;
      color: #FFFFFF;
      font-weight: bold;
      font-size: 11.5pt;
  }}
  .info-table td:first-child {{
      font-weight: bold;
      background-color: #EFF6FF;
      color: #1E3A8A;
      width: 22%;
      border-left: 2px solid #93C5FD;
  }}
  .summary-strip td {{
      background-color: #F8FAFC;
      text-align: center;
      width: 25%;
      padding: 12px 10px;
      border-top: 4px solid #1E3A8A;
  }}
  .summary-strip td:nth-child(1) {{ background-color: #EFF6FF; border-top-color: #2563EB; }}
  .summary-strip td:nth-child(2) {{ background-color: #F0FDFA; border-top-color: #0D9488; }}
  .summary-strip td:nth-child(3) {{ background-color: #F0FDF4; border-top-color: #16A34A; }}
  .summary-strip td:nth-child(4) {{ background-color: #FFFBEB; border-top-color: #D97706; }}
  .summary-strip .label {{
      font-size: 10.5pt;
      color: #475569;
      font-weight: bold;
      display: block;
      margin-bottom: 4px;
  }}
  .summary-strip .val {{
      font-size: 13.5pt;
      font-weight: bold;
      color: #0F172A;
  }}
  .core-grid td {{
      width: 33.33%;
      background-color: #F1F5F9;
      color: #1E3A8A;
      font-weight: bold;
      text-align: center;
      padding: 11px;
      font-size: 12pt;
  }}
  .score-cell {{
      text-align: center;
      font-weight: bold;
      color: #1E3A8A;
      background-color: #EFF6FF !important;
      font-size: 11.5pt;
  }}
  ul {{ margin-top: 6px; margin-bottom: 8px; padding-right: 22px; }}
  li {{ margin-bottom: 8px; page-break-inside: avoid; }}
</style>
</head>
<body>{cleaned_body}</body>
</html>"""


def main():
    try:
        send_telegram(f"🚀 بدء معالجة حلقة: {CLEAN_NAME}\n⚙️ {MODE_TITLE}", use_html=False)
        log_step(f"انطلاق معالجة الحلقة: {CLEAN_NAME} | {MODE_TITLE}")

        _ = get_prioritized_keys()

        drive_files = []
        external_url, url_source = (None, None)
        for wait_attempt in range(10):
            folder_meta = get_drive_folder_metadata()
            folder_desc = folder_meta.get("description", "") or ""
            all_files = list_drive_folder_contents()
            drive_files = [f for f in all_files if f.get("mimeType", "").startswith("video/") or f.get("mimeType", "").startswith("audio/")]
            drive_files.sort(key=lambda x: x["name"])

            external_url, url_source = find_video_url_in_folder(all_files, folder_desc=folder_desc)
            if external_url or drive_files:
                break
            if wait_attempt == 0:
                log_step("⏳ بانتظار ظهور رابط الفيديو في وصف المجلد (Folder Description) أو اكتمال رفع الملف (مهلة سماح 45 ثانية)...")
            time.sleep(5)

        if not external_url and not drive_files:
            raise RuntimeError(
                "المجلد لا يحتوي على رابط فيديو في وصف المجلد (Folder Description) ولا مستند Google Doc ولا ملف نصي ولا ملف فيديو مرفوع."
            )

        segments, exact_duration = prepare_media_segments(drive_files, external_url=external_url, url_source=url_source)
        total_segs_count = len(segments)

        result = evaluate_with_gemini(segments, exact_duration, total_segs_count)

        log_step(f"✅ تم بنجاح تحليل كامل مقاطع الحلقة ({total_segs_count} جزء) بمدة ({exact_duration})!")

        main_html = wrap_html(f"تقييم تفصيلي - {CLEAN_NAME}", result.get("thinkingBanner", "") + result["mainDetailedHtml"])
        sum_html = wrap_html(f"تقرير مختصر - {CLEAN_NAME}", result.get("thinkingBanner", "") + result["summarizedHtml"])

        last_spoken = result.get("lastSpokenWords", "غير متوفر").strip()
        elapsed = int(time.time() - START_TIME)
        elapsed_str = f"{elapsed // 60} دقيقة و {elapsed % 60} ثانية"

        cost_info = result.get("costInfo", {
            "keyLabel": "FREE",
            "modelUsed": "gemini-3.8-flash",
            "isPaid": False,
            "processCost": 0.0,
            "totalMonthly": get_monthly_spend()
        })

        actual_cost = round(float(cost_info.get("processCost", 0.0)), 4)
        monthly_tot = round(float(cost_info.get("totalMonthly", get_monthly_spend())), 4)

        summary_json_data = {
            "status": "SUCCESS",
            "folder_name": CLEAN_NAME,
            "editorial_type": EDITORIAL_TYPE_LABEL,
            "mode_title": MODE_TITLE,
            "level_name": MODE_TITLE,
            "model_used": cost_info.get("modelUsed", ""),
            "key_label": cost_info["keyLabel"],
            "processCost": actual_cost,
            "process_cost_usd": actual_cost,
            "episode_budget_usd": EPISODE_MAX_COST,
            "monthly_spend_usd": monthly_tot,
            "monthly_cap_usd": MAX_MONTHLY_BUDGET,
            "duration_hms": exact_duration,
            "segments_count": total_segs_count,
            "elapsed_str": elapsed_str,
            "last_spoken_words": last_spoken
        }

        save_bulletproof_backups(main_html, sum_html, summary_json_data)

        upload_report_to_drive(f"01_Main_Detailed_Report_{CLEAN_NAME}.html", main_html, "text/html")
        upload_report_to_drive(f"02_Summarized_Report_{CLEAN_NAME}.html", sum_html, "text/html")
        upload_report_to_drive("_run_summary.json", json.dumps(summary_json_data, ensure_ascii=False, indent=2), "application/json")

        summary_desc = (
            f"COST_USD: ${actual_cost:.4f} | MONTHLY_USD: ${monthly_tot:.4f}\n"
            f"• نوع التقييم: {EDITORIAL_TYPE_LABEL}\n"
            f"• المنظومة المختارة: {MODE_TITLE}\n"
            f"• الحساب والنموذج المستخدم: {cost_info['keyLabel']} ({cost_info.get('modelUsed', '')})\n"
            f"• تكلفة هذه العملية: ${actual_cost:.4f} (سقف الحلقة المحدد: ${EPISODE_MAX_COST:.2f})\n"
            f"• إجمالي الإنفاق هذا الشهر: ${monthly_tot:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
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
            f"⚖️ المعيار: {EDITORIAL_TYPE_LABEL}\n"
            f"⚙️ المنظومة: {MODE_TITLE}\n"
            f"🔑 الحساب المستخدم: {cost_info['keyLabel']} ({cost_info.get('modelUsed', '')})\n"
            f"💵 تكلفة العملية الحالية: ${actual_cost:.4f} (سقف الحلقة: ${EPISODE_MAX_COST:.2f})\n"
            f"📊 إجمالي الإنفاق هذا الشهر: ${monthly_tot:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"⏱️ المدة: {exact_duration} | الاستغراق: {elapsed_str}\n\n"
            f"🎙️ آخر كلام في الحلقة:\n«{last_spoken}»\n\n"
            f"📁 رابط المجلد:\n{folder_url}",
            use_html=False
        )
    except Exception as e:
        log_step("❌ توقفت المعالجة بسبب خطأ.")
        error_msg = str(e)
        current_monthly = get_monthly_spend()
        fail_summary = {
            "status": "FAILED",
            "folder_name": CLEAN_NAME,
            "editorial_type": EDITORIAL_TYPE_LABEL,
            "mode_title": MODE_TITLE,
            "process_cost_usd": 0.0,
            "monthly_spend_usd": current_monthly,
            "monthly_cap_usd": MAX_MONTHLY_BUDGET,
            "error_reason": error_msg
        }
        save_bulletproof_backups(None, None, fail_summary)
        upload_report_to_drive("_run_summary.json", json.dumps(fail_summary, ensure_ascii=False, indent=2), "application/json")

        fail_desc = (
            f"• نوع التقييم: {EDITORIAL_TYPE_LABEL}\n"
            f"• المنظومة المختارة: {MODE_TITLE}\n"
            f"• إجمالي الإنفاق هذا الشهر: ${current_monthly:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"• تفاصيل السبب:\n{error_msg[:1500]}"
        )
        update_drive_folder(f"FAILED_{CLEAN_NAME}", fail_desc)

        raw_fail_msg = (
            f"❌ فشل معالجة الحلقة: {CLEAN_NAME}\n"
            f"⚖️ المعيار: {EDITORIAL_TYPE_LABEL}\n"
            f"⚙️ المنظومة: {MODE_TITLE}\n"
            f"📊 إجمالي الإنفاق هذا الشهر: ${current_monthly:.4f} / ${MAX_MONTHLY_BUDGET:.2f}\n"
            f"⚠️ تفاصيل الخطأ:\n{error_msg[:3000]}"
        )
        send_telegram(raw_fail_msg, use_html=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
