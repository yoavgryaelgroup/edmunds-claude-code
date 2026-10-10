# WhatsApp booking assistant – Dr. Tova Averch dental clinic (prototype)

Patients chat with the clinic on WhatsApp in Hebrew or English; an AI assistant (Claude) books, moves and cancels appointments
in the clinic's Google Calendar, sends reminders, keeps a waitlist, and hands medical or urgent matters to the staff.

Design: see the shared page "WhatsApp Booking Assistant — Dr. Tova Averch Dental Clinic".

```
Patient ── WhatsApp ── Meta Cloud API ──► /webhook ──► AI assistant (Claude + booking tools)
                                                         │
                         booking rules (one chair, 10-min buffer, urgent holds, holidays)
                                                         │
                                                  Google Calendar
```

## What is in it

| File | What it does |
| --- | --- |
| `clinic.yaml` | Everything about the clinic: hours, treatments and lengths, buffer, urgent slots, holidays, templates |
| `app/agent.py` | The assistant: Hebrew instructions for Claude and its tools (find / book / move / cancel / waitlist / handoff) |
| `app/scheduling.py` | Free slots and bookings; re-checks every slot under a lock, so there is no double booking |
| `app/calendars.py` | Google Calendar (events carry the patient's phone, type and status), or an in-memory calendar for the demo |
| `app/whatsapp.py` | Sending messages and templates, reading webhooks, checking Meta's signature, staff alerts, waitlist offers |
| `app/main.py` | The web server: `/webhook` for WhatsApp, `/demo` chat page that works without WhatsApp |
| `app/reminders.py` | Hourly job: reminders 24 h ahead; releases new patients' bookings never confirmed |
| `app/store.py` | SQLite: patients (name, birth date, consent), recent chats, waitlist, handoffs, audit log |
| `tests/` | 14 tests of the rules, tools, webhook and reminders (no internet needed) |

Clinic rules built in (change them in `clinic.yaml`):

- Hours: Sunday and Monday 14:00–18:00, Wednesday and Thursday 09:00–13:00, Friday 08:00–12:00; Tuesday and
  Saturday closed; closed on major Jewish holidays (from Google's holiday calendar), eves close at 12:00.
- One chair, 10 minutes of cleaning after every appointment, start times on a 15-minute grid, at least 2 hours' notice.
- One 30-minute urgent slot per session (Sun/Mon 16:00, Wed/Thu 11:00, Fri 10:00), released 3 hours before.
- Root canals and whitening start in the first hour of a session.
- New patients book a first visit (45 min) or an urgent slot; it stays "pending" until they confirm in the reminder.
- The assistant never diagnoses or gives medical advice; severe symptoms go to staff at once with the clinic phone.
- Hebrew and English: it answers in the patient's language (Hebrew by default), switches when they do, and sends
  reminders and waitlist offers in that language. English names and clinic details are in `clinic.yaml` (`*_en`).

## 1. Try the demo on your PC (10 minutes, no WhatsApp or Google needed)

Needs Python 3.11 or later and a Claude API key (https://console.anthropic.com → API keys). In PowerShell:

```powershell
cd C:\path\to\edmunds-claude-code\dental-whatsapp-assistant
python -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000/demo and write as a patient would, for example:

- `שלום, אפשר לקבוע תור לניקוי שיניים?`
- `אני מטופלת חדשה, רוצה לקבוע בדיקה` (it will ask for name, birth date and consent)
- `יש לי כאב חזק בשן ונפיחות` (urgent: earliest urgent slot, staff alert)
- `אפשר להזיז את התור שלי ליום רביעי?`

The panel on the right shows the calendar and the staff handoffs. The demo calendar is in memory: it empties when
the server stops. Change the "patient number" at the top to play a second patient.

## 2. Connect the clinic's Google Calendar

1. Go to https://console.cloud.google.com, create a project, and enable the **Google Calendar API**.
2. *IAM & Admin → Service accounts → Create*; then *Keys → Add key → JSON*. Save the file as
   `service-account.json` in this folder (it is never committed: see `.gitignore`).
3. In Google Calendar, open the clinic calendar's *Settings and sharing → Share with specific people*, add the
   service account's e-mail (…@….iam.gserviceaccount.com) with **Make changes to events**.
4. Copy the *Calendar ID* (Settings → Integrate calendar) and set:

```powershell
$env:GOOGLE_CALENDAR_ID = "....@group.calendar.google.com"
$env:GOOGLE_CREDENTIALS_FILE = "service-account.json"
```

Restart the server; `/demo` now says "מחובר ל-Google Calendar" and bookings appear in the clinic calendar (yellow
while pending, green when confirmed). Staff keep using Google Calendar as before: any event they add blocks that
time, and an event they move or delete is seen by the assistant. To mark someone as an existing patient (so they can
book any type), set `existing_patient = 1` for their number in the `patient` table of `assistant.db`.

Tip: start with a **copy** of the clinic calendar for testing.

## 3. Connect WhatsApp

1. Create a Meta developer app (https://developers.facebook.com → My apps → Create app → Business) and add the
   **WhatsApp** product. Meta gives a test number to start with. For the real assistant, verify the business and
   add the assistant's number, **055-957-9423** (+972 55-957-9423), under *WhatsApp → API setup → Add phone number*;
   Meta sends a code to it by SMS or voice call. Don't install WhatsApp on that number (a number on the Cloud API
   can't be used in the app). Display name: the clinic's name, as Meta approves it.
2. From *WhatsApp → API setup* copy the **Phone number ID** and create a **permanent access token** (System user in
   Business settings). From *App settings → Basic* copy the **App secret**.
3. The server must be reachable from the internet over HTTPS. For a demo, run `ngrok http 8000` and use the https
   address it prints; for real use, host it (any small cloud server or service that runs Python).
4. *WhatsApp → Configuration → Webhook*: callback URL `https://<your address>/webhook`, verify token = the text you
   chose for `WHATSAPP_VERIFY_TOKEN`; subscribe to **messages**.

```powershell
$env:WHATSAPP_TOKEN = "EAAG..."
$env:WHATSAPP_PHONE_NUMBER_ID = "1234567890"
$env:WHATSAPP_VERIFY_TOKEN = "choose-any-long-random-text"
$env:WHATSAPP_APP_SECRET = "abc123..."
```

You can also put all of these in a `.env` file (copy `.env.example`).

### Message templates (submit in Meta, language Hebrew)

WhatsApp only allows free text within 24 hours of the patient's last message; reminders and offers need approved
templates (category *Utility*). Names must match `clinic.yaml`:

| Name | Body | Buttons (quick reply) |
| --- | --- | --- |
| `appointment_reminder` (Hebrew) | שלום {{1}}, תזכורת לתור שלך ל{{2}} במרפאת השיניים, {{3}}. נשמח לאישור הגעה. | מאשר/ת · לשנות מועד · לבטל |
| `appointment_reminder` (English) | Hi {{1}}, a reminder of your {{2}} appointment at the dental clinic, {{3}}. Please confirm you're coming. | Confirm · Reschedule · Cancel |
| `waitlist_offer` (Hebrew) | התפנה תור ל{{1}}, {{2}}. רוצה אותו? אפשר להשיב להודעה זו ונקבע. | רוצה · לא תודה |
| `waitlist_offer` (English) | A {{1}} appointment just opened up: {{2}}. Would you like it? Reply to this message and we'll book it. | Yes please · No thanks |
| `staff_alert` (Hebrew only) | פנייה מהעוזר הדיגיטלי: מטופל {{1}} – {{2}} | – |

Create each template once and add both languages to it (Hebrew and English); the assistant sends each patient the
version in their language.

Staff alerts go to the numbers in `staff_alert_numbers` in `clinic.yaml`: the clinic phone, 051-564-6322. That
number keeps using the WhatsApp (Business) app as today, so the assistant needs its **own** number on the Cloud API:
a number registered on the Cloud API can no longer be used in the WhatsApp app, and can't send alerts to itself.

### Reminders

Run the hourly job with Windows Task Scheduler (or cron): `python -m app.reminders` in this folder, with the same
environment variables. It needs Google Calendar connected (the demo calendar lives only inside the server). In the
demo you can trigger it with `curl.exe -X POST http://localhost:8000/demo/run-reminders`.

## Before real patients use it

- Fill the TODOs in `clinic.yaml` (Hebrew name spelling, address, parking).
- A privacy review: patient data (name, birth date, phone, appointments) is health-related under Israel's Privacy
  Protection Law. Write the privacy notice the assistant asks patients to accept, decide how long chats are kept, and
  keep `assistant.db` and the keys on a protected machine.
- Read a week of conversations with the staff before switching the number over fully.
- `/demo` pages have no login: set `DEMO_ENABLED=0` on a public server.

## Tests

```powershell
python -m pytest -q
```
