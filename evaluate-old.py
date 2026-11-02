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

# تحديد نوع المنظومة تلقائياً من لاحقة اسم المجلد (_FREE أو _PAID أو _LOW)
UPPER_NAME = CLEAN_NAME.upper()
FORCE_FREE = UPPER_NAME.endswith("_FREE") or "_FREE_" in UPPER_NAME
FORCE_PAID = (UPPER_NAME.endswith("_PAID") or "_PAID_" in UPPER_NAME) and not FORCE_FREE
IS_LOW_MODE = UPPER_NAME.endswith("_LOW") or "_LOW_" in UPPER_NAME

if FORCE_FREE and IS_LOW_MODE:
    MODE_TITLE = "المنظومة المجانية الإجبارية الاقتصادية (_LOW_FREE - بدون أي سحب مالي)"
elif FORCE_FREE:
    MODE_TITLE = "المنظومة المجانية الإجبارية (_FREE - تتوقف فوراً إذا لم يتوفر رصيد مجاني)"
elif FORCE_PAID and IS_LOW_MODE:
    MODE_TITLE = "المنظومة المدفوعة المباشرة الاقتصادية (_LOW_PAID)"
elif FORCE_PAID:
    MODE_TITLE = "المنظومة المدفوعة المباشرة (_PAID - الدقة القصوى)"
elif IS_LOW_MODE:
    MODE_TITLE = "المنظومة الاقتصادية (_LOW) (صورة كل 5 ثوان) [تبدأ بالمجاني ثم المدفوع عند الفشل]"
else:
    MODE_TITLE = "المنظومة الاحترافية (الدقة القصوى - صورة كل ثانية) [تبدأ بالمجاني ثم المدفوع عند الفشل]"

START_TIME = time.time()
LOG_LINES = []

# إعدادات سقف الإنفاق الشهري (5.00$) وسقف تكلفة الحلقة الواحدة المسبق (الافتراضي 0.50$ = 50 سنت)
MAX_MONTHLY_BUDGET = float(os.environ.get("MAX_MONTHLY_BUDGET", "5.00"))
DEFAULT_MAX_EPISODE_COST = float(os.environ.get("MAX_EPISODE_COST", "0.50"))
SPEND_TRACKER_FILENAME = "_gemini_monthly_spend_tracker.json"

def parse_episode_max_cost(folder_clean_name, default_cap=DEFAULT_MAX_EPISODE_COST):
    """
    استخراج السقف الأقصى لتكلفة الحلقة من اسم المجلد إن وجد:
    أمثلة مدعومة:
    - _max50c أو _50c أو _cap30c -> 0.50$ أو 0.30$
    - _max0.40 أو _cap0.75 -> 0.40$ أو 0.75$
    - _max1.2$ أو _max100c -> 1.20$ أو 1.00$
    - _max40 (الرقم أكبر من 5 بدون علامة $) -> يعتبره سنتات (40 سنت = 0.40$)
    """
    upper = folder_clean_name.upper()
    m_cents = re.search(r'_(?:MAX|CAP)?(\d+(?:\.\d+)?)C(?:ENTS)?(?=_|$)', upper)
    if m_cents:
        cents = float(m_cents.group(1))
        return round(cents / 100.0, 4), f"محدد من اسم المجلد ({cents:g} سنت = ${cents/100.0:.2f})"
    
    m_max = re.search(r'_(?:MAX|CAP)(\d+(?:\.\d+)?)\$?(?=_|$)', upper)
    if m_max:
        val = float(m_max.group(1))
        if val > 5.0 and "$" not in m_max.group(0):
            return round(val / 100.0, 4), f"محدد من اسم المجلد ({val:g} سنت = ${val/100.0:.2f})"
        return round(val, 4), f"محدد من اسم المجلد (${val:.2f})"
    
    return round(default_cap, 4), f"السقف الافتراضي للحلقة (${default_cap:.2f})"

EPISODE_MAX_COST, EPISODE_COST_SOURCE = parse_episode_max_cost(CLEAN_NAME)

# ==============================================================================
# المعايير والتعليمات والقوالب الكاملة لمهارة تقييم مقدمي البرامج (SKILL EMBEDDED)
# ==============================================================================
SKILL_INSTRUCTIONS_AND_RUBRIC = """
أنت خبير متخصص في تقييم الأداء المهني والتلفزيوني لمقدمي البرامج السياسية والحوارية وفق السلم المعتمد (90 نقطة).
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

3. مراعاة الخط التحريري وسياق القناة (قاعدة جوهرية لا يجوز مخالفتها):
   - يُجرى التقييم مع الأخذ في الحسبان الهوية التحريرية للقناة (إعلام داعم ومتبنٍّ لخط المقاومة).
   - يُمنع منعاً باتاً خصم علامات أو انتقاد المقدم بسبب تبنيه لخط المقاومة أو استخدامه مصطلحاتها (مثل: كيان العدو، المقاومة، الشهيد، الاعتداءات الإسرائيلية) أو عدم لعبه دور "المحايد" تجاه العدو؛ فهذا التزام طبيعي بالسياسة التحريرية للقناة وليس عيباً مهنياً.
   - التمايز بين "المونولوج العبثي" و"التوظيف التحريري الهادف": إذا كان الضيف حليفاً وفي مسار تفكيك لسرديات خصوم المقاومة، فإن ترك المساحة له قد يكون قراراً تحريرياً مقصوداً.
   - حدود التوظيف التحريري: المونولوج الذي يتجاوز 3 دقائق متصلة دون أي مقاطعة وظيفية أو سؤال تلخيصي أو توجيهي من المقدم يُعد فشلاً تقنياً في إدارة الإيقاع التلفزيوني (Broken TV Rhythm)، ويجب المحاسبة عليه بخصم النقاط في معايير "التعامل مع الضيف"، "المتابعة والتعقيب"، و"ضبط الوقت".
   - في معيار "الاختصار والحياد وتجنب تعدد الأسئلة"، يُقصد بالحياد المهني هنا: عدم مصادرة رأي الضيف بمقدمات تقريرية مطولة تفرض عليه الإجابة، وليس الحياد السياسي تجاه العدو.

4. العمق التحليلي والشواهد الحية الموثقة (إلزامي في التقرير المفصل):
   - يُمنع منعاً باتاً الاكتفاء بعبارات عامة أو ملخصات قصيرة! يجب أن تكون الملاحظات المهنية داخل الجداول غنية، ناقدة، ومفصلة، ومدعومة بـ:
     أ) اقتباسات نصية حرفية (Verbatim Quotes) من كلام المقدم مع توقيتها الدقيق (مثلاً: رصد الانزلاقات إلى اللهجة العامية، العثرات اللفظية، صياغة الأسئلة الطويلة/المركبة، أو الأسئلة التتبعية الذكية).
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
ثالثاً: القالب الإلزامي الحرفي لمخرجات الـ HTML (template.md)
================================================================================
1. يجب أن يحتوي حقل `mainDetailedHtml` على كود HTML الكامل لـ "التقرير الرئيسي المفصل" مطابقاً تماماً لهذا الهيكل وجميع جداوله الـ 6 الفرعية وجدول الأمثلة الموثقة (10 أمثلة على الأقل موزعة على كامل الحلقة):

<h1>تقييم تفصيلي وفق المعايير الموسعة</h1>
<div class="subtitle">[اسم المقدم] - [الشهر والسنة / عنوان الحلقة]</div>

<table class="info-table">
    <tr><td>المقدم</td><td>[اسم المقدم]</td></tr>
    <tr><td>الحلقة</td><td>[نوع الحلقة / البرنامج - التاريخ]</td></tr>
    <tr><td>الضيوف</td><td>[أسماء الضيوف والمراسلين وطريقة المشاركة: استديو / Zoom / مداخلة ميدانية]</td></tr>
    <tr><td>المادة المسجلة</td><td>[تفاصيل التقارير VT أو المداخلات وتوقيتها من 00:00:00 إلى 00:00:00 أو لا يوجد]</td></tr>
    <tr><td>مدة الحلقة</td><td>[المدة الدقيقة الكاملة بالثواني والدقائق - مع تأكيد تحليلها بالكامل]</td></tr>
</table>

<h2>الخلاصة التنفيذية</h2>
<p>[فقرة تحليلية مكثفة ومعمقة من 5-8 أسطر تلخص المستوى العام للمقدم في الحلقة، أبرز ملامح الأداء في إدارة الحوار والأسئلة والمتابعة والحضور البصري واللغوي، ونقطة الضعف أو الاستنزاف الزمني الأساسية]</p>

<table class="summary-strip">
    <tr>
        <td><span class="label">العلامة</span><span class="val">[ ] من 90</span></td>
        <td><span class="label">النسبة</span><span class="val">[ ]%</span></td>
        <td><span class="label">أبرز نقطة قوة</span><span class="val">[توصيف مركز]</span></td>
        <td><span class="label">أولوية التطوير</span><span class="val">[توصيف مركز]</span></td>
    </tr>
</table>

<h2>منهج التقييم</h2>
<p>تم تطبيق السلم المعتمد: إدارة الحلقة 20، التعامل مع الضيف 20، جودة الأسئلة 20، المتابعة والتعقيب 15، الحضور والأداء 10، وضبط الوقت 5. التقييم مهني وتقني مع مراعاة الخط التحريري، وجرى استخدام التفريغ الموقّت والفيديو للتحقق من الانتقالات والحضور البصري على كامل مدة الحلقة.</p>

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
    <tr><td>بناء وتسلسل الملفات</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>إغلاق الملف والانتقال</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>السيطرة على المسار والمرونة</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>الافتتاح والخاتمة</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
</table>

<h2>2. التعامل مع الضيف - [ ]/20</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>الإصغاء واحترام الجواب</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>توزيع المساحة وعدم المقاطعة غير الضرورية</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>المقاطعة الضرورية ومواجهة الضيف</td><td>[ ]/5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>احتواء المونولوج وإعادة المسار</td><td>[ ]/5</td><td>[تحليل مفصل مع تطبيق قاعدة الـ 3 دقائق والخط التحريري بالتوقيتات]</td></tr>
</table>

<h2>3. جودة الأسئلة - [ ]/20</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>وضوح السؤال</td><td>[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>دقة السؤال</td><td>[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>عمق السؤال</td><td>[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>الارتباط بالمحور</td><td>[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>الاختصار والحياد وتجنب تعدد الأسئلة</td><td>[ ]/4</td><td>[تحليل طول الأسئلة والأسئلة المركبة مع ذكر مدتها وتوقيتها واقتباسها]</td></tr>
</table>

<h2>4. المتابعة والتعقيب - [ ]/15</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>المتابعة المبنية على الجواب</td><td>[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>التقاط النقاط المهمة والتناقضات</td><td>[ ]/4</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>طلب التوضيح والتحقق</td><td>[ ]/3.5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
    <tr><td>بناء السؤال التالي من الجواب</td><td>[ ]/3.5</td><td>[تحليل مفصل مدعوم بالشواهد والتوقيتات]</td></tr>
</table>

<h2>5. الحضور والأداء - [ ]/10</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>الجاذبية التلفزيونية</td><td>[ ]/3</td><td>[تحليل مفصل للهيئة والحضور والاتزان على الشاشة]</td></tr>
    <tr><td>لغة الجسد والتواصل البصري</td><td>[ ]/2.5</td><td>[رصد بصري دقيق بالتوقيتات لحركة اليدين، النظرات، لمس الوجه/اللحية، استخدام القلم، والجلسة]</td></tr>
    <tr><td>الصوت والإلقاء</td><td>[ ]/2.5</td><td>[تحليل الخامة الصوتية، التنغيم، مخارج الحروف، والنفس بالتوقيتات]</td></tr>
    <tr><td>اللغة والطلاقة</td><td>[ ]/2</td><td>[رصد الفصحى والانزلاقات إلى العامية أو التلعثم مع إيراد اقتباسات حرفية بالتوقيتات]</td></tr>
</table>

<h2>6. ضبط الوقت - [ ]/5</h2>
<table>
    <tr><th style="width: 25%;">المعيار الفرعي</th><th style="width: 10%;">العلامة</th><th style="width: 65%;">الملاحظة المهنية والشواهد الموثقة</th></tr>
    <tr><td>توزيع الوقت بين الملفات والضيوف</td><td>[ ]/1.7</td><td>[تحليل مفصل لتوزيع الزمن بين المحاور والضيوف والمراسلين]</td></tr>
    <tr><td>منع استنزاف الوقت في نقطة واحدة</td><td>[ ]/1.6</td><td>[تحليل مفصل مدعوم بالتوقيتات]</td></tr>
    <tr><td>إنهاء المحاور والخاتمة</td><td>[ ]/1.7</td><td>[تحليل دقة إغلاق الحلقة في التوقيت الختامي]</td></tr>
</table>

<h2>أمثلة موثقة من الحلقة</h2>
<table>
    <tr><th style="width: 22%;">التوقيت</th><th style="width: 33%;">المثال (اقتباس حرفي أو رصد بصري)</th><th style="width: 45%;">الدلالة المهنية</th></tr>
    <tr><td>من 00:00:00 إلى 00:00:00</td><td>[وصف الموقف أو الاقتباس الحرفي]</td><td>[تحليل الدلالة المهنية إيجاباً أو سلباً وربطها بالمعيار]</td></tr>
    <!-- أدرج ما لا يقل عن 10 أمثلة موثقة موزعة على كامل مدة الحلقة من بدايتها لختامها -->
</table>

<h2>نقاط القوة الرئيسية</h2>
<ul>
    <li>[نقطة قوة مفصلة 1 مع التوقيت]</li>
    <li>[نقطة قوة مفصلة 2 مع التوقيت]</li>
    <li>[نقطة قوة مفصلة 3 مع التوقيت]</li>
    <li>[نقطة قوة مفصلة 4 مع التوقيت]</li>
</ul>

<h2>أولويات التطوير</h2>
<ul>
    <li>[أولوية تطوير عملية 1 مع الشاهد الزمني]</li>
    <li>[أولوية تطوير عملية 2 مع الشاهد الزمني]</li>
    <li>[أولوية تطوير عملية 3 مع الشاهد الزمني]</li>
    <li>[أولوية تطوير عملية 4 مع الشاهد الزمني]</li>
</ul>

<h2>الخلاصة النهائية</h2>
<p>العلامة الحالية: [ ] من 90 ([ ]%). [فقرة ختامية مركزة تلخص مستوى الأداء العام والخطوة التطويرية الأهم لرفع العلامة مستقبلاً].</p>

---
2. ويجب أن يحتوي حقل `summarizedHtml` على كود HTML الكامل لـ "التقرير المختصر" مطابقاً تماماً لهذا الهيكل:

<h1>نموذج تقييم أداء مقدم/ة برنامج (تقرير مختصر)</h1>
<table class="info-table">
    <tr><td>اسم المقدم/ة</td><td>[اسم المقدم/ة]</td></tr>
    <tr><td>التاريخ</td><td>[التاريخ]</td></tr>
    <tr><td>البرنامج / نوع الحلقة</td><td>[البرنامج / نوع الحلقة]</td></tr>
    <tr><td>الضيوف والمشاركون</td><td>[الضيوف والمشاركون]</td></tr>
    <tr><td>المواد المسجلة</td><td>[المواد المسجلة]</td></tr>
    <tr><td>مدة الحلقة</td><td>[مدة الحلقة]</td></tr>
</table>

<h2>الخلاصة التنفيذية</h2>
<p>[الخلاصة التنفيذية]</p>

<h2>النتيجة الإجمالية</h2>
<table>
    <tr><th style="width: 25%;">العلامة</th><th style="width: 20%;">النسبة</th><th style="width: 55%;">التوصيف المختصر</th></tr>
    <tr><td><strong>[ ] من 90</strong></td><td><strong>[ ]%</strong></td><td>[التوصيف المختصر]</td></tr>
</table>

<h2>العلامات الأساسية</h2>
<table>
    <tr><th style="width: 25%;">المعيار</th><th style="width: 15%;">العلامة</th><th style="width: 60%;">ملاحظة مختصرة</th></tr>
    <tr><td>إدارة الحلقة</td><td>[ ] من 20</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td>التعامل مع الضيف</td><td>[ ] من 20</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td>جودة الأسئلة</td><td>[ ] من 20</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td>المتابعة والتعقيب</td><td>[ ] من 15</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td>الحضور والأداء</td><td>[ ] من 10</td><td>[ملاحظة مختصرة]</td></tr>
    <tr><td>ضبط الوقت</td><td>[ ] من 5</td><td>[ملاحظة مختصرة]</td></tr>
    <tr style="font-weight: bold; background-color: #D9E1F2;">
        <td>المجموع الكلي</td><td>[ ] من 90</td><td>[خلاصة عامة]</td>
    </tr>
</table>

<h2>نقاط القوة الرئيسية</h2>
<ul>
    <li>[نقطة قوة 1]</li>
    <li>[نقطة قوة 2]</li>
    <li>[نقطة قوة 3]</li>
</ul>

<h2>أولويات التطوير</h2>
<ul>
    <li>[أولوية تطوير 1]</li>
    <li>[أولوية تطوير 2]</li>
    <li>[أولوية تطوير 3]</li>
</ul>
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

def get_model_rates(model_name, in_tokens):
    if "pro" in model_name.lower():
        in_rate = (1.25 / 1_000_000) if in_tokens <= 200_000 else (2.50 / 1_000_000)
        out_rate = (10.00 / 1_000_000) if in_tokens <= 200_000 else (15.00 / 1_000_000)
    else:
        in_rate = 0.50 / 1_000_000
        out_rate = 2.50 / 1_000_000
    return in_rate, out_rate

def calculate_call_cost(model_name, usage_metadata):
    in_tokens = int(usage_metadata.get("promptTokenCount", 0) or 0)
    out_tokens = int(usage_metadata.get("candidatesTokenCount", 0) or 0)
    think_tokens = int(usage_metadata.get("thoughtsTokenCount", 0) or 0)
    total_out = out_tokens + think_tokens
    in_rate, out_rate = get_model_rates(model_name, in_tokens)
    cost = (in_tokens * in_rate) + (total_out * out_rate)
    return max(cost, 0.0001)

def count_input_tokens_pre_flight(model_name, system_prompt, contents, api_key, fallback_duration_sec=3600.0):
    """
    حساب العدد الدقيق لتوكنز الإدخال مجاناً عبر countTokens API قبل إرسال طلب التوليد الفعلي
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:countTokens?key={api_key}"
    payload = {
        "generateContentRequest": {
            "model": f"models/{model_name}",
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents
        }
    }
    try:
        res = session.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            total_t = int(res.json().get("totalTokens", 0) or 0)
            if total_t > 0:
                return total_t
    except Exception:
        pass
    # حساب احتياطي دقيق مبني على مدة الحلقة بالثواني (حوالي 295 توكن لكل ثانية صوت+صورة + 4500 توكن للتعليمات)
    return int((fallback_duration_sec * 295) + 4500)

def estimate_pre_flight_cost(model_name, in_tokens, thinking_budget, expected_report_tokens=9500):
    """
    تقدير التكلفة المسبقة للعملية قبل التنفيذ بناءً على عدد توكنز الإدخال الفعلي وميزانية التفكير
    """
    in_rate, out_rate = get_model_rates(model_name, in_tokens)
    estimated_thinking = int(thinking_budget * 0.45) if thinking_budget > 0 else 0
    estimated_total_out = expected_report_tokens + estimated_thinking
    est_cost = (in_tokens * in_rate) + (estimated_total_out * out_rate)
    return round(est_cost, 4)

def get_prioritized_keys():
    """
    ترتيب المفاتيح:
    - المفتاح الأخير في GEMINI_API_KEYS يعتبر هو المفتاح المدفوع (PAID).
    - جميع المفاتيح التي قبله تعتبر مفاتيح مجانية (FREE #1, FREE #2, ...).
    - إذا كان اسم المجلد ينتهي بـ _free: يستخدم المفاتيح المجانية فقط، وإذا فشلت جميعها يعطي FAILED ولا يذهب للمدفوع أبداً.
    - إذا كان اسم المجلد ينتهي بـ _paid: يذهب مباشرة للمفتاح المدفوع فقط.
    - الوضع الافتراضي: يجرب المفاتيح المجانية بالترتيب، وإذا فشلت ينتقل للمفتاح المدفوع.
    """
    if not API_KEYS:
        raise RuntimeError("لا توجد مفاتيح API معرفة في GEMINI_API_KEYS.")

    if len(API_KEYS) == 1:
        if FORCE_FREE:
            log_step("🆓 وضع المجاني الإجباري (_free): تجربة المفتاح الوحيد المتاح كمفتاح مجاني...")
            return [(1, "FREE (Key #1)", API_KEYS[0], False)]
        only_type = "PAID (Key #1)" if FORCE_PAID else "Key #1"
        return [(1, only_type, API_KEYS[0], FORCE_PAID)]

    paid_key = API_KEYS[-1]
    paid_idx = len(API_KEYS)
    free_keys = API_KEYS[:-1]

    if FORCE_FREE:
        log_step(f"🆓 اسم المجلد ينتهي بـ _free: حصر المعالجة بالمفاتيح المجانية فقط (عددها {len(free_keys)}) ومنع استخدام المفتاح المدفوع...")
        ordered_free = []
        for idx, fk in enumerate(free_keys, start=1):
            ordered_free.append((idx, f"FREE (Key #{idx})", fk, False))
        return ordered_free

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
    m = re.search(r'(https?://[^\s<>"\']+)', text)
    if m:
        return m.group(1).strip().rstrip('.,;)]}')
    return None

def find_video_url_in_folder(all_files):
    """
    البحث عن رابط فيديو خارجي في حال عدم وجود ملف فيديو مرفوع داخل المجلد:
    1. داخل أي ملف نصي (.txt / .url) أو مستند Google Doc داخل المجلد (باستثناء ملف السجل 00_).
    2. داخل خانة الوصف (Description) الخاصة بمجلد Google Drive نفسه.
    """
    headers = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    for f in all_files:
        fname = f.get("name", "")
        if fname.startswith("00_") or fname.endswith(".html"):
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
                        return url, f"مستند Google Doc ({fname})"
            elif mime.startswith("text/") or fname.lower().endswith((".txt", ".url", ".webloc", ".md")):
                r = session.get(
                    f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media",
                    headers=headers,
                    timeout=20
                )
                if r.status_code == 200:
                    url = extract_url_from_text(r.text)
                    if url:
                        return url, f"الملف النصي ({fname})"
        except Exception:
            pass

    # فحص وصف المجلد (Folder Description)
    try:
        r = session.get(
            f"https://www.googleapis.com/drive/v3/files/{FOLDER_ID}",
            headers=headers,
            params={"fields": "description"},
            timeout=20
        )
        if r.status_code == 200:
            desc = r.json().get("description", "")
            url = extract_url_from_text(desc)
            if url and "drive.google.com/drive/folders/" not in url:
                return url, "وصف المجلد (Folder Description)"
    except Exception:
        pass

    return None, None

def upload_binary_file_to_drive_folder(local_path, drive_filename, mime_type="video/mp4"):
    """رفع ملف الفيديو المسحوب من الرابط إلى مجلد Google Drive مباشرة عبر Resumable Upload"""
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

def download_video_from_external_url(url, local_path):
    """
    سحب الفيديو مباشرة من أي رابط خارجي إلى السيرفر السحابي:
    - يدعم روابط Google Drive المشتركة
    - يدعم روابط MP4 المباشرة
    - يدعم روابط يوتيوب، فيسبوك، تويتر، M3U8، ومواقع القنوات عبر yt-dlp
    """
    # 1. إذا كان الرابط لملف على Google Drive
    gdrive_match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]{20,})', url)
    if gdrive_match and "drive.google.com" in url:
        file_id = gdrive_match.group(1)
        log_step(f"☁️ الرابط هو ملف Google Drive - جاري السحب المباشر...")
        try:
            download_drive_file(file_id, local_path)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
                return
        except Exception as e:
            log_step(f"⚠️ تعذر السحب المباشر عبر API ({str(e)[:40]})، جاري المحاولة عبر yt-dlp...")

    # 2. إذا كان رابط MP4 مباشر
    is_direct_mp4 = url.lower().split("?")[0].endswith((".mp4", ".mov", ".mkv", ".mp3", ".m4a"))
    if is_direct_mp4:
        try:
            log_step("🌐 جاري التحميل المباشر لملف الفيديو من الرابط...")
            with session.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "").lower()
                if "text/html" not in ctype:
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    if os.path.exists(local_path) and os.path.getsize(local_path) > 100_000:
                        return
        except Exception as e:
            log_step(f"⚠️ التحويل إلى محرك السحب الذكي (yt-dlp) بعد تعذر التحميل التقليدي ({str(e)[:40]})...")

    # 3. استخدام محرك yt-dlp الشامل (يدعم المواقع، يوتيوب، M3U8، والروابط المباشرة)
    log_step("⚙️ تجهيز محرك السحب السحابي (yt-dlp) وسحب الحلقة من الرابط...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=False)
    if os.path.exists(local_path):
        os.remove(local_path)

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--no-check-certificates",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", local_path,
        url
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(local_path):
        err_tail = (res.stderr or res.stdout or "خطأ غير معروف")[-300:]
        raise RuntimeError(f"فشل سحب الفيديو من الرابط ({url}):\n{err_tail}")

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

def prepare_media_segments(drive_files, external_url=None, url_source=None):
    local_raw = "raw_episode.mp4"
    if external_url:
        log_step(f"🔗 تم العثور على رابط للحلقة في {url_source}:\n{external_url}")
        download_video_from_external_url(external_url, local_raw)
        size_mb = os.path.getsize(local_raw) / (1024 * 1024)
        log_step(f"✅ اكتمل سحب الفيديو من الرابط بنجاح ({size_mb:.1f} MB)!")

        # حفظ نسخة من ملف الـ MP4 المسحوب داخل مجلد Google Drive تلقائياً (إلا إذا كتب المستخدم _nosave)
        if "_NOSAVE" not in UPPER_NAME:
            try:
                drive_mp4_name = f"{CLEAN_NAME}.mp4"
                log_step(f"☁️ جاري حفظ نسخة من الفيديو ({drive_mp4_name} - {size_mb:.1f} MB) داخل مجلد Google Drive...")
                upload_binary_file_to_drive_folder(local_raw, drive_mp4_name, mime_type="video/mp4")
                log_step("✅ تم حفظ ملف الـ MP4 داخل مجلد Google Drive بنجاح!")
            except Exception as up_e:
                log_step(f"⚠️ تعذر حفظ نسخة الـ MP4 في الدرايف ({str(up_e)[:50]})، جاري متابعة التقييم...")

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

    if len(drive_files) == 1 and drive_files[0]["mimeType"].startswith("video/"):
        raw_file = drive_files[0]
        size_mb = int(raw_file.get("size", 0)) / (1024 * 1024)
        log_step(f"📥 سحب ملف الفيديو الخام ({raw_file['name']} - {size_mb:.1f} MB)...")
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
        ("gemini-3.8-flash",       8192,  "gemini-3.8-flash (Med Thinking 8192)",       False),
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

        user_instruction = (
            f"قم بتحليل وتقييم حلقة ({CLEAN_NAME}) بالكامل من 00:00:00 إلى {exact_duration} "
            "وفق السلم المعتمد (90 نقطة - 6 معايير أساسية و23 معياراً فرعياً) والقوالب المعتمدة في التعليمات، "
            "مع الالتزام التام بمراعاة الخط التحريري للقناة، وإيراد الاقتباسات الحرفية والشواهد البصرية للغة الجسد بالتوقيتات الدقيقة."
        )
        full_contents = [{"role": "user", "parts": prompt_parts + [{"text": user_instruction}]}]

        # إذا كان المفتاح مدفوعاً: نفحص عدد التوكنز الفعلي والتكلفة المتوقعة لكل نموذج مسبقاً قبل الدفع!
        in_tokens_count = None
        if is_paid_key:
            in_tokens_count = count_input_tokens_pre_flight(
                models[0][0], system_prompt, full_contents, api_key, fallback_duration_sec=accum_sec
            )
            remaining_monthly = round(MAX_MONTHLY_BUDGET - get_monthly_spend(), 4)
            effective_cap = min(EPISODE_MAX_COST, remaining_monthly)
            log_step(
                f"🧮 الفحص المسبق لتكلفة الحلقة كاملة ({ len(segments) } أجزاء مجتمعة = {in_tokens_count:,} توكن) | "
                f"سقف الحلقة الكلي المسموح: ${EPISODE_MAX_COST:.2f} ({EPISODE_COST_SOURCE}) | "
                f"المتبقي من سقف الشهر: ${remaining_monthly:.2f}"
            )

        skipped_due_to_cost = []
        for model_name, budget, label, is_highest in models:
            if is_paid_key and in_tokens_count is not None:
                est_cost = estimate_pre_flight_cost(model_name, in_tokens_count, budget)
                remaining_monthly = round(MAX_MONTHLY_BUDGET - get_monthly_spend(), 4)
                effective_cap = min(EPISODE_MAX_COST, remaining_monthly)
                if est_cost > effective_cap:
                    skip_msg = f"تخطي {label}: التكلفة المقدرة (${est_cost:.3f}) أعلى من السقف المحدد للحلقة (${effective_cap:.2f})"
                    log_step(f"💸 {skip_msg} - الانتقال لنموذج أوفر يناسب السقف...")
                    skipped_due_to_cost.append(f"• {label}: مقدر بـ ${est_cost:.3f} (السقف ${effective_cap:.2f})")
                    continue
                else:
                    log_step(f"✅ النموذج {label} يطابق سقف التكلفة المحدد (التكلفة المقدرة: ~${est_cost:.3f} <= ${effective_cap:.2f})...")

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

        if is_paid_key and skipped_due_to_cost:
            errors.append(
                f"⛔ تم إيقاف السحب المالي قبل التنفيذ لأن جميع النماذج تتجاوز السقف المحدد للحلقة (${EPISODE_MAX_COST:.2f}):\n"
                + "\n".join(skipped_due_to_cost)
            )

    raise RuntimeError("\n".join(errors))

# ==============================================================================
# المسار الثاني: نسخة (_LOW) - تحليل كل مقطع على حدة مع التبديل الذكي للمفاتيح
# ==============================================================================
def call_gemini_low_mode_step(system_prompt, user_parts_builder, models_list, step_title, expect_json=True, current_ep_spent=0.0):
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
            if current_ep_spent >= EPISODE_MAX_COST:
                ep_ceil_msg = (
                    f"تم إيقاف العملية: وصلت التكلفة التراكمية لأجزاء الحلقة (${current_ep_spent:.4f}) "
                    f"إلى السقف الكلي المحدد للحلقة (${EPISODE_MAX_COST:.2f})."
                )
                log_step(f"🛑 {ep_ceil_msg}")
                errors.append(f"• {key_label}: {ep_ceil_msg}")
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

    # تقدير التكلفة الإجمالية لكامل أجزاء الحلقة في الوضع الاقتصادي (_LOW) في حال اللجوء للمفتاح المدفوع
    est_total_low_tokens = int((accum_sec * 180) + (num_parts * 4500))
    est_total_low_cost = round((est_total_low_tokens * (0.50 / 1_000_000)) + ((num_parts + 1) * 4000 * (2.50 / 1_000_000)), 4)
    if FORCE_PAID and est_total_low_cost > EPISODE_MAX_COST:
        raise RuntimeError(
            f"⛔ تم إيقاف العملية قبل السحب المالي: التكلفة الإجمالية المقدرة لجميع أجزاء الحلقة ({num_parts * 2} أجزاء) "
            f"في الوضع الاقتصادي هي ~${est_total_low_cost:.3f} وهي أعلى من السقف المحدد للحلقة (${EPISODE_MAX_COST:.2f})."
        )

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
            "2. استخرج تقريراً تحليلياً تفصيلياً وشاملاً لهذا المقطع يغطي المعايير الـ 6 الأساسية والـ 23 معياراً فرعياً، مع تدوين الاقتباسات الحرفية (للأسئلة، المقاطعات، العامية) والرصد البصري الدقيق للغة الجسد بتوقيتاتها المتصلة.\n"
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

        if not is_last_part:
            log_step("⏳ تهدئة 35 ثانية قبل المقطع التالي لإراحة عداد التوكنز...")
            time.sleep(35)

    log_step("📊 دمج تحليلات جميع المقاطع وإصدار التقرير النهائي الموحد (90 نقطة)...")
    combined_notes = "\n\n".join(part_summaries)

    final_sys_prompt = (
        f"{SKILL_INSTRUCTIONS_AND_RUBRIC}\n\n"
        f"أمامك التحليلات التفصيلية الكاملة لجميع مقاطع الحلقة ({CLEAN_NAME}) والبالغة مدتها الإجمالية ({exact_duration}).\n"
        "قواعد إجبارية صارمة:\n"
        f"1. قم بدمج وتوحيد هذه التحليلات في تقييم نهائي واحد متكامل وشامل للحلقة كاملة من (00:00:00) حتى ({exact_duration}) وفق المعايير الستة الأساسية والـ 23 معياراً فرعياً والقوالب المعتمدة.\n"
        "2. يمنع نهائياً كتابة عبارات مثل (في الجزء الأول أو المقطع الثاني)، بل تعامل مع الحلقة كوحدة متصلة بالتوقيتات المطلقة.\n"
        "3. استخرج النص الحرفي لآخر كلام قيل في ختام الحلقة مع توقيته المتصل وضعه في حقل lastSpokenWords.\n"
        "4. أرجع كائن JSON حصراً بالحقول التالية:\n"
        "   - isFullDurationAnalyzed: (boolean) ضعه true\n"
        "   - analyzedAllSegments: (boolean) ضعه true\n"
        "   - lastSpokenWords: (string) النص الحرفي لآخر كلام قيل في ختام الحلقة مع التوقيت المتصل.\n"
        "   - mainDetailedHtml: (string) التقرير الرئيسي المفصل والمنسق بـ HTML وفق القالب المعتمد.\n"
        "   - summarizedHtml: (string) التقرير المختصر والمنسق بـ HTML وفق القالب المعتمد."
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

def clean_inner_html(raw_html):
    """إزالة وسوم html/head/body المكررة إن قام النموذج بإضافتها داخل الحقل"""
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
  @page {{ size: A4; margin: 2cm; }}
  body {{ font-family: "Arial", sans-serif; font-size: 12.5pt; line-height: 1.6; direction: rtl; text-align: right; color: #111111; max-width: 950px; margin: 30px auto; padding: 20px; background-color: #ffffff; }}
  .low-mode-banner {{ background-color: #FFF3CD; color: #856404; border: 2px solid #FFEEBA; border-right: 6px solid #FFC107; padding: 12px 16px; margin-bottom: 18px; border-radius: 4px; font-size: 11.5pt; }}
  .thinking-note {{ font-size: 8.5pt; color: #666666; text-align: left; border-bottom: 1px dotted #cccccc; padding-bottom: 4px; margin-bottom: 12px; }}
  h1 {{ text-align: center; font-size: 18pt; font-weight: bold; color: #1F4E79; margin-bottom: 4px; page-break-after: avoid; }}
  .subtitle {{ text-align: center; font-size: 13pt; font-weight: bold; color: #595959; margin-top: 0; margin-bottom: 18px; page-break-after: avoid; }}
  h2 {{ font-size: 14pt; font-weight: bold; color: #1F4E79; margin-top: 20px; margin-bottom: 8px; border-bottom: 1.5px solid #1F4E79; padding-bottom: 4px; page-break-after: avoid; }}
  p {{ margin-top: 6px; margin-bottom: 10px; text-align: justify; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; page-break-inside: avoid; }}
  table, th, td {{ border: 1px solid #444444; }}
  th, td {{ padding: 8px 10px; text-align: right; vertical-align: top; page-break-inside: avoid; }}
  tr {{ page-break-inside: avoid; page-break-after: auto; }}
  th {{ background-color: #D9E1F2; color: #1F4E79; font-weight: bold; }}
  .info-table td:first-child {{ font-weight: bold; background-color: #F2F2F2; width: 22%; }}
  .summary-strip td {{ background-color: #F9FAFB; text-align: center; width: 25%; }}
  .summary-strip .label {{ font-size: 10.5pt; color: #595959; display: block; }}
  .summary-strip .val {{ font-size: 12.5pt; font-weight: bold; color: #1F4E79; }}
  .core-grid td {{ width: 33.33%; background-color: #F9F9F9; font-weight: bold; }}
  ul {{ margin-top: 6px; margin-bottom: 14px; padding-right: 22px; }}
  li {{ margin-bottom: 6px; page-break-inside: avoid; }}
</style>
</head>
<body>{cleaned_body}</body>
</html>"""

def main():
    try:
        send_telegram(f"🚀 بدء معالجة حلقة: {CLEAN_NAME}\n⚙️ {MODE_TITLE}", use_html=False)
        log_step(f"انطلاق معالجة الحلقة: {CLEAN_NAME} | {MODE_TITLE}")
        
        all_files = list_drive_folder_contents()
        drive_files = [f for f in all_files if f.get("mimeType", "").startswith("video/") or f.get("mimeType", "").startswith("audio/")]
        drive_files.sort(key=lambda x: x["name"])

        external_url, url_source = (None, None)
        if not drive_files:
            external_url, url_source = find_video_url_in_folder(all_files)
            if not external_url:
                raise RuntimeError(
                    "المجلد لا يحتوي على ملف فيديو/صوت ولا يحتوي على رابط فيديو خارجي (في مستند Google Doc أو ملف نصي أو وصف المجلد)."
                )

        segments, exact_duration = prepare_media_segments(drive_files, external_url=external_url, url_source=url_source)
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
            f"• تكلفة هذه العملية: ${cost_info['processCost']:.4f} (سقف الحلقة المحدد: ${EPISODE_MAX_COST:.2f})\n"
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
            f"💵 تكلفة العملية الحالية: ${cost_info['processCost']:.4f} (سقف الحلقة: ${EPISODE_MAX_COST:.2f})\n"
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
