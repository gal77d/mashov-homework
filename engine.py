"""
מנוע שיעורי בית ממשו"ב
-----------------------
בודק את שיעורי הבית במשו"ב. כשיש שיעור בית חדש: בונה דף שאלות ותשובות (עם Groq או Claude),
מצלם אותו לתמונה ושולח אותה למייל.

שימוש:
  python engine.py            בדיקה רגילה (fetch + process)
  python engine.py fetch      רק בודק במשו"ב ושומר את החדשים ל-pending.json
  python engine.py process    פותר את מה שב-pending.json ושולח מייל
  python engine.py demo       ניסיון בלי משו"ב, עם שיעור בית לדוגמה
  python engine.py latest 2   פותר את 2 שיעורי הבית האחרונים במשו"ב גם אם כבר נשלחו
אפשר להוסיף --no-mail כדי לא לשלוח מייל (רק לשמור את הדף והתמונה בתיקייה out).
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen.json"
PENDING_FILE = ROOT / "pending.json"
OUT = ROOT / "out"
SITE = ROOT / "docs"
MAX_SITE_ITEMS = 60
MASHOV_API = "https://web.mashov.info/api"


# ---------------------------------------------------------------- settings
def load_dotenv():
    """קורא את .env (רק במחשב; בענן ההגדרות מגיעות מ-GitHub Secrets)."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env(name, default=None, required=False):
    v = os.environ.get(name, "").strip()
    if not v:
        if required:
            sys.exit(f"חסרה ההגדרה {name} (בקובץ .env או ב-GitHub Secrets)")
        return default
    return v


def school_year():
    # שנת הלימודים במשו"ב היא השנה שבה היא נגמרת (תשפ"ז = 2027)
    t = dt.date.today()
    return t.year + 1 if t.month >= 8 else t.year


# ---------------------------------------------------------------- mashov
def mashov_login():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"
    body = {
        "semel": int(env("MASHOV_SCHOOL", required=True)),
        "year": int(env("MASHOV_YEAR", school_year())),
        "username": env("MASHOV_USER", required=True),
        "password": env("MASHOV_PASS", required=True),
        "IsBiometric": False,
        "appName": "info.mashov.students",
        "apiVersion": "3.20210425",
        "appVersion": "3.20210425",
        "appBuild": "3.20210425",
        "deviceUuid": "chrome",
        "devicePlatform": "chrome",
        "deviceManufacturer": "windows",
        "deviceModel": "desktop",
        "deviceVersion": "130",
    }
    r = s.post(f"{MASHOV_API}/login", json=body, timeout=30)
    if r.status_code != 200:
        sys.exit(f"ההתחברות למשו\"ב נכשלה (קוד {r.status_code}). בדוק סמל מוסד, שם משתמש וסיסמה.\n{r.text[:300]}")
    csrf = r.headers.get("x-csrf-token")
    if csrf:
        s.headers["x-csrf-token"] = csrf
    data = r.json()
    cred = data.get("credential") or {}
    uid = cred.get("userId")
    # חשבון הורה: לוקחים את הילד הראשון (או לפי MASHOV_CHILD)
    children = (data.get("accessToken") or {}).get("children") or []
    if children and (cred.get("userType") == 1 or not uid):
        want = env("MASHOV_CHILD", "")
        kid = next((c for c in children if want and want in json.dumps(c, ensure_ascii=False)), children[0])
        uid = kid.get("childGuid") or kid.get("studentGuid") or uid
    if not uid:
        sys.exit("התחברתי למשו\"ב אבל לא מצאתי מזהה תלמיד בתשובה.")
    return s, uid


def mashov_homework():
    s, uid = mashov_login()
    items = homework_items(s, uid)
    try:
        items += inbox_items(s)
    except Exception as e:  # אם הדואר לא עובד, ממשיכים עם שיעורי הבית
        print(f"  ! לא הצלחתי לקרוא דואר נכנס: {e}")
    items.sort(key=lambda x: x["date"])
    return items


def homework_items(s, uid):
    r = s.get(f"{MASHOV_API}/students/{uid}/homework", timeout=30)
    if r.status_code != 200:
        sys.exit(f"לא הצלחתי לקרוא שיעורי בית (קוד {r.status_code}).\n{r.text[:300]}")
    rows = r.json()
    dates = sorted((h.get("lessonDate") or "")[:10] for h in rows)
    print(f"  עמודת שיעורי בית: {len(rows)} שורות, האחרונה: {dates[-1] if dates else '-'}")
    items = []
    for h in rows:
        text = (h.get("homework") or "").strip()
        if not text:
            continue
        items.append({
            "subject": h.get("subjectName") or "",
            "date": (h.get("lessonDate") or "")[:10],
            "text": text,
            "key": hw_key(h, text),
        })
    return items


def strip_html(t):
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", t or "")
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def msg_body(m):
    for k in ("body", "content", "text", "messageBody", "html"):
        if isinstance(m.get(k), str) and m[k].strip():
            return strip_html(m[k])
    return ""


def inbox_items(s):
    """הודעות מ"דואר נכנס" במשו"ב. ה-AI מחליט אם יש בהן משימה."""
    r = s.get(f"{MASHOV_API}/mail/inbox/conversations", params={"skip": 0, "take": 30}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"קוד {r.status_code}")
    convs = r.json()
    # ביומן מדפיסים רק מבנה ומספרים, אף פעם לא תוכן (הריפו ציבורי)
    print(f"  דואר נכנס: {len(convs)} שיחות" + (f", שדות: {sorted(convs[0].keys())}" if convs else ""))
    items = []
    shown = False
    for c in convs:
        cid = c.get("conversationId") or c.get("id")
        msgs = c.get("messages") or []
        if cid and not any(msg_body(m) for m in msgs):
            # ברשימה אין את גוף ההודעה - פותחים את השיחה עצמה
            d = s.get(f"{MASHOV_API}/mail/conversations/{cid}", timeout=30)
            if d.status_code == 200:
                full = d.json() or {}
                msgs = full.get("messages", []) if isinstance(full, dict) else full
            elif not shown:
                print(f"  ! פתיחת שיחה: קוד {d.status_code}")
        if msgs and not shown:
            shown = True
            print(f"  שדות הודעה: {sorted(msgs[0].keys())}")
        for m in msgs or []:
            body = msg_body(m)
            if not body:
                continue
            mid = m.get("messageId") or m.get("id") or f"{cid}|{m.get('sendTime')}"
            sender = m.get("senderName") or c.get("senderName") or ""
            subj = c.get("subject") or ""
            items.append({
                "subject": f"דואר: {subj}"[:60] if subj else "דואר נכנס",
                "date": (m.get("sendTime") or c.get("sendTime") or "")[:10],
                "text": (f"מאת: {sender}\n" if sender else "") + body[:4000],
                "key": "mail:" + hashlib.sha1(str(mid).encode()).hexdigest()[:14],
                "mail": True,
            })
    return items


def hw_key(h, text):
    base = f'{h.get("lessonId")}|{h.get("groupId")}|{h.get("lessonDate")}|{text}'
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- AI
SYSTEM = """אתה מורה פרטי מעולה לתלמיד בבית ספר בישראל. מקבלים שיעור בית כפי שהמורה כתב אותו במשו"ב.
המשימה: לבנות דף שאלות ותשובות בעברית.

כללים:
- אם השאלות כתובות בטקסט: העתק כל שאלה, ותן תשובה נכונה והסבר קצר וברור של הדרך.
- אם כתוב רק הפניה לספר/חוברת (למשל "עמ' 45 תרגילים 3-7") ואי אפשר לדעת את השאלות:
  כתוב זאת ב-note, וזהה את הנושא הסביר; ואז תן 3 שאלות לדוגמה באותו נושא עם תשובות, וסמן כל אחת בשדה "example": true.
- אם זו הודעת דואר שאין בה שום משימה, שיעורי בית או עבודה להגשה (למשל הודעה כללית, טיול, אסיפת הורים),
  החזר רק {"skip": true}.
- אם השאלה נותנת בחירה (למשל "בחרו דמות", "לבחירתכם", "תנו דוגמה משלכם", "כתבו על חוויה אישית"):
  אסור לכתוב "תלוי בבחירתכם" או להשאיר לתלמיד להשלים. בחר בעצמך אפשרות טובה ומתאימה מהידע שלך,
  כתוב בתשובה מה בחרת, וענה עליה מלא ומוכן להגשה.
- כתוב מתמטיקה בטקסט פשוט (×, ÷, ², √, ½, ≤), בלי LaTeX.
- תשובות מדויקות. אם אינך בטוח, כתוב זאת.

החזר JSON בלבד, במבנה:
{"title": "כותרת קצרה", "note": "הערה (או מחרוזת ריקה)",
 "questions": [{"q": "השאלה", "answer": "התשובה", "explanation": "הסבר", "example": false}]}"""


def user_prompt(hw):
    if hw.get("mail"):
        return f"הודעה מ'דואר נכנס' במשו\"ב.\nנושא: {hw['subject']}\nתאריך: {hw['date']}\n{hw['text']}"
    return f"מקצוע: {hw['subject']}\nתאריך השיעור: {hw['date']}\nשיעור הבית כפי שנכתב במשו\"ב:\n{hw['text']}"


def ask_groq(hw):
    key = env("GROQ_API_KEY", required=True)
    model = env("GROQ_MODEL", "openai/gpt-oss-120b")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt(hw)}],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def ask_claude(hw):
    key = env("ANTHROPIC_API_KEY", required=True)
    model = env("CLAUDE_MODEL", "claude-sonnet-5-5")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "max_tokens": 4000,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": user_prompt(hw)}],
        },
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude {r.status_code}: {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json()["content"])


def solve(hw):
    provider = env("AI_PROVIDER", "groq").lower()
    try:
        raw = ask_claude(hw) if provider == "claude" else ask_groq(hw)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        data.setdefault("questions", [])
        return data
    except Exception as e:  # לא מפילים את כל הריצה בגלל שיעור אחד
        print(f"  ! הפתרון נכשל: {e}")
        return {"title": hw["subject"], "note": f"לא הצלחתי לפתור הפעם ({e.__class__.__name__}).", "questions": []}


# ---------------------------------------------------------------- page
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f3f1ec;font-family:"Noto Sans Hebrew","Segoe UI",Arial,sans-serif;color:#1d1d1f;direction:rtl}
.wrap{max-width:760px;margin:0 auto;padding:28px 22px}
h1{font-size:26px;margin:0 0 4px}
.sub{color:#6b6860;font-size:14px;margin-bottom:22px}
.hw{background:#fff;border-radius:14px;padding:20px 22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:6px}
.subject{font-size:20px;font-weight:700;color:#1f4fa3}
.date{color:#6b6860;font-size:13px;white-space:nowrap}
.orig{background:#f7f6f2;border-right:4px solid #c9c4b6;padding:10px 12px;border-radius:6px;white-space:pre-wrap;font-size:15px;margin:8px 0 12px}
.note{background:#fff6dc;border-radius:8px;padding:9px 12px;font-size:14px;margin-bottom:12px}
.q{border-top:1px solid #ece9e1;padding:12px 0 4px}
.q:first-of-type{border-top:0}
.qt{font-weight:700;font-size:16px;white-space:pre-wrap}
.ex{display:inline-block;background:#e9e3ff;color:#4a32a8;font-size:11px;border-radius:99px;padding:1px 8px;margin-inline-start:6px;font-weight:600}
.ans{margin-top:6px;font-size:16px;white-space:pre-wrap}
.ans b{color:#137a3e}
.why{margin-top:4px;color:#4b4943;font-size:14px;white-space:pre-wrap}
.orig,.qt,.ans,.why,.note{unicode-bidi:plaintext;text-align:start}
@media print{
body{background:#fff}
.wrap{max-width:none;padding:0}
.hw{box-shadow:none;border:1px solid #ddd;padding:14px 16px}
.q{break-inside:avoid}
}
"""


def build_page(results, heading="שיעורי בית חדשים"):
    e = html.escape
    now = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    cards = []
    for hw, sol in results:
        qs = []
        for i, q in enumerate(sol.get("questions", []), 1):
            tag = '<span class="ex">דוגמה</span>' if q.get("example") else ""
            qs.append(
                f'<div class="q"><div class="qt">{i}. {e(str(q.get("q", "")))}{tag}</div>'
                f'<div class="ans"><b>תשובה:</b> {e(str(q.get("answer", "")))}</div>'
                + (f'<div class="why">{e(str(q.get("explanation", "")))}</div>' if q.get("explanation") else "")
                + "</div>"
            )
        note = f'<div class="note">{e(sol["note"])}</div>' if sol.get("note") else ""
        cards.append(
            f'<div class="hw"><div class="head"><span class="subject">{e(hw["subject"])}</span>'
            f'<span class="date">{e(hw["date"])}</span></div>'
            f'<div class="orig">{e(hw["text"])}</div>{note}{"".join(qs)}</div>'
        )
    return (
        '<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>שיעורי בית</title><style>{CSS}</style></head><body><div class=\"wrap\">"
        f'<h1>{e(heading)}</h1><div class="sub">נוצר ב-{now} · {len(results)} שיעורים</div>'
        f'{"".join(cards)}</div></body></html>'
    )


def screenshot(page_path, png_path, pdf_path=None):
    """צילום הדף לתמונה, ואם ביקשו - גם קובץ PDF להדפסה (A4)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! playwright לא מותקן, ממשיך בלי תמונה")
        return None
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 760, "height": 600}, device_scale_factor=2)
        pg.goto(page_path.as_uri())
        if png_path:
            pg.screenshot(path=str(png_path), full_page=True)
        if pdf_path:
            pg.emulate_media(media="print")
            pg.pdf(path=str(pdf_path), format="A4", print_background=True,
                   margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
        b.close()
    return png_path or pdf_path


# ---------------------------------------------------------------- mail
def send_mail(results, page_html, png):
    to = env("MAIL_TO", required=True)
    user = env("SMTP_USER", required=True)
    msg = EmailMessage()
    subjects = ", ".join(dict.fromkeys(hw["subject"] for hw, _ in results))
    msg["Subject"] = f"שיעורי בית חדשים: {subjects}"
    msg["From"] = env("MAIL_FROM", user)
    msg["To"] = to
    msg.set_content("שיעורי בית חדשים - התמונה מצורפת.")
    msg.add_alternative(page_html, subtype="html")
    if png:
        msg.add_attachment(Path(png).read_bytes(), maintype="image", subtype="png", filename="homework.png")
    host = env("SMTP_HOST", "smtp.gmail.com")
    port = int(env("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, env("SMTP_PASS", required=True))
        s.send_message(msg)
    print(f"  נשלח מייל אל {to}")


# ---------------------------------------------------------------- flow
def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return None


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")


def set_output(name, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def cmd_fetch():
    fix_file_names()
    items = mashov_homework()
    seen = load_seen()
    if seen is None:
        # ריצה ראשונה: לא שולחים את כל ההיסטוריה, רק מסמנים אותה
        save_seen({h["key"] for h in items})
        print(f"ריצה ראשונה: סימנתי {len(items)} שיעורי בית קיימים. מעכשיו יישלחו רק חדשים.")
        set_output("new", 0)
        return []
    if not any(k.startswith("mail:") for k in seen):
        # פעם ראשונה עם דואר: לא פותרים את כל ההודעות הישנות
        # (הודעות מ-4 הימים האחרונים כן נפתרות)
        cutoff = (dt.date.today() - dt.timedelta(days=4)).isoformat()
        mail_keys = {h["key"] for h in items if h.get("mail") and h["date"] < cutoff}
        if mail_keys:
            seen |= mail_keys
            save_seen(seen)
            print(f"סימנתי {len(mail_keys)} הודעות דואר קיימות")
    new = [h for h in items if h["key"] not in seen]
    print(f"נמצאו {len(items)} שיעורי בית, מתוכם {len(new)} חדשים")
    PENDING_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    set_output("new", len(new))
    return new


def file_stem(key):
    # ":" אסור בשמות קבצים בווינדוס
    return key.replace(":", "-")


def fix_file_names():
    """משנה שמות קבצים ישנים עם ":" (נוצרו לפני התיקון)."""
    data_file = SITE / "data" / "homework.json"
    if not data_file.exists():
        return
    entries = json.loads(data_file.read_text(encoding="utf-8"))
    changed = False
    for it in entries:
        for f in ("img", "pdf"):
            p = it.get(f) or ""
            if ":" in p:
                new = p.replace(":", "-")
                if (SITE / p).exists():
                    (SITE / p).rename(SITE / new)
                it[f] = new
                changed = True
    if changed:
        data_file.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


def save_to_site(results):
    """מוסיף את התוצאות לאתר (docs/data/homework.json + תמונה לכל שיעור)."""
    data_file = SITE / "data" / "homework.json"
    img_dir = SITE / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    data_file.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(data_file.read_text(encoding="utf-8")) if data_file.exists() else []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for hw, sol in results:
        if hw.get("mail"):
            # האתר ציבורי: לא מפרסמים את ההודעה עצמה (שם השולח, נושא, גוף ההודעה), רק את המשימה שנפתרה
            hw = dict(hw, subject="משימה מהדואר", text=sol.get("title") or "משימה מהודעה במשו\"ב")
        single = OUT / f"{hw['key']}.html"
        single.write_text(build_page([(hw, sol)], heading=hw["subject"]), encoding="utf-8")
        stem = file_stem(hw["key"])
        png = screenshot(single, img_dir / f"{stem}.png", img_dir / f"{stem}.pdf")
        entries = [e for e in entries if e["key"] != hw["key"]]
        entries.insert(0, {
            "key": hw["key"], "subject": hw["subject"], "date": hw["date"], "text": hw["text"],
            "added": now, "title": sol.get("title", ""), "note": sol.get("note", ""),
            "questions": sol.get("questions", []),
            "img": f"img/{stem}.png" if png else "",
            "pdf": f"img/{stem}.pdf" if png else "",
        })
    keep = entries[:MAX_SITE_ITEMS]
    for old in entries[MAX_SITE_ITEMS:]:  # מוחקים תמונות וקבצים ישנים
        for f in ("img", "pdf"):
            if old.get(f):
                (SITE / old[f]).unlink(missing_ok=True)
    data_file.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  נשמר באתר: {len(results)} שיעורים")


def cmd_pdfs():
    data_file = SITE / "data" / "homework.json"
    entries = json.loads(data_file.read_text(encoding="utf-8")) if data_file.exists() else []
    OUT.mkdir(exist_ok=True)
    made = 0
    for it in entries:
        if it.get("pdf") and (SITE / it["pdf"]).exists():
            continue
        hw = {k: it[k] for k in ("key", "subject", "date", "text")}
        sol = {k: it.get(k) for k in ("title", "note", "questions")}
        single = OUT / f"{it['key']}.html"
        single.write_text(build_page([(hw, sol)], heading=hw["subject"]), encoding="utf-8")
        pdf = SITE / "img" / f"{file_stem(it['key'])}.pdf"
        if screenshot(single, None, pdf):
            it["pdf"] = f"img/{file_stem(it['key'])}.pdf"
            made += 1
    data_file.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"נוצרו {made} קבצי PDF")


def cmd_process(items, mail=True, mark=True):
    if not items:
        print("אין מה לפתור")
        return
    results = []
    for hw in items:
        print(f"- פותר: {'הודעת דואר' if hw.get('mail') else hw['subject']} ({hw['date']})")
        sol = solve(hw)
        if sol.get("skip"):
            print("  (הודעה בלי משימה, מדלג)")
            continue
        results.append((hw, sol))
    OUT.mkdir(exist_ok=True)
    if results:
        save_to_site(results)
    # מייל רק אם הוגדרה סיסמת מייל (לא חובה)
    if mail and env("SMTP_PASS"):
        page = build_page(results)
        page_path = OUT / "homework.html"
        page_path.write_text(page, encoding="utf-8")
        send_mail(results, page, screenshot(page_path, OUT / "homework.png"))
    if mark:
        seen = load_seen() or set()
        save_seen(seen | {hw["key"] for hw in items})
    if PENDING_FILE.exists():
        PENDING_FILE.unlink()


DEMO = [
    {"subject": "מתמטיקה", "date": str(dt.date.today()), "key": "demo1",
     "text": "1. פתרו: 3x + 5 = 20\n2. מצאו את שטח המלבן שאורכו 7 ס\"מ ורוחבו 4 ס\"מ\n3. חשבו: 2/3 + 1/4"},
    {"subject": "אנגלית", "date": str(dt.date.today()), "key": "demo2",
     "text": "Workbook page 34, exercises 2-3 (past simple)"},
]


def main():
    load_dotenv()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mail = "--no-mail" not in sys.argv
    cmd = args[0] if args else "run"
    if cmd == "fetch":
        cmd_fetch()
    elif cmd == "process":
        items = json.loads(PENDING_FILE.read_text(encoding="utf-8")) if PENDING_FILE.exists() else []
        cmd_process(items, mail)
    elif cmd == "demo":
        cmd_process(DEMO, mail, mark=False)
    elif cmd == "redo":  # מהענן: פותר מחדש את N האחרונים (בלי מייל)
        n = int(args[1]) if len(args) > 1 else 1
        cmd_process(mashov_homework()[-n:], mail=False)
    elif cmd == "pdfs":  # יוצר PDF לשיעורים שכבר באתר ואין להם עדיין
        cmd_pdfs()
    elif cmd == "latest":
        n = int(args[1]) if len(args) > 1 else 1
        cmd_process(mashov_homework()[-n:], mail)
    else:
        cmd_process(cmd_fetch(), mail)


if __name__ == "__main__":
    main()
