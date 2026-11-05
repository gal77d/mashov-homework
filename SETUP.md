# הקמת מנוע שיעורי הבית

## 1. ניסיון במחשב
1. העתק את `.env.example` לקובץ חדש בשם `.env` ומלא בו את הפרטים בעצמך.
2. בדיקה מול משו"ב בלי מייל (פותר את שיעור הבית האחרון):
   `python engine.py latest 1 --no-mail` ← התוצאה בתיקייה `out`
3. בדיקה עם מייל: `python engine.py latest 1`

## 2. סיסמה לשליחת מייל (SMTP_PASS)
זו לא הסיסמה הרגילה, אלא "סיסמת אפליקציה":
- **iCloud:** appleid.apple.com ← כניסה ואבטחה ← סיסמאות ספציפיות ליישום.
  הגדרות: `SMTP_HOST=smtp.mail.me.com`, `SMTP_PORT=587`, `SMTP_USER=` כתובת ה-iCloud שלך.
- **Gmail:** myaccount.google.com/apppasswords (צריך אימות דו-שלבי).
  הגדרות: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`.

## 3. העלאה לענן (GitHub)
1. ליצור ריפו **פרטי** (Private) ולהעלות אליו את התיקייה (בלי `.env`).
2. Settings ← Secrets and variables ← Actions:
   - **Secrets:** `MASHOV_SCHOOL`, `MASHOV_USER`, `MASHOV_PASS`, `GROQ_API_KEY`, `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`
   - **Variables** (רק אם לא Gmail): `SMTP_HOST`, `SMTP_PORT`
3. Actions ← "בדיקת שיעורי בית" ← Run workflow.
   הריצה הראשונה רק מסמנת את שיעורי הבית הקיימים. מעכשיו כל שיעור חדש יישלח למייל.
   הבדיקה רצה אוטומטית כל 30 דקות בין 12:00 ל-23:30.

## 4. מעבר ל-Claude
מוסיפים Secret בשם `ANTHROPIC_API_KEY` ו-Variable בשם `AI_PROVIDER` עם הערך `claude`.
