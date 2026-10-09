"""The AI assistant: Claude with booking tools, one conversation per WhatsApp number.

The model only ever acts for the number that sent the message: the phone is never a tool argument, so a patient
can't see or change anyone else's appointments.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

from .config import env
from .scheduling import BookingError, hebrew_label

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
KEEP_MESSAGES = 40
FORGET_AFTER = timedelta(hours=24)   # a new chat starts fresh after a day of silence

TOOLS = [
    {"name": "find_patient",
     "description": "Who is writing: whether this WhatsApp number is registered, the patient's name, whether they are "
                    "an existing patient of the clinic, their upcoming appointments (with ids) and waitlist entries. "
                    "Call it at the start of every conversation.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "register_patient",
     "description": "Register a new patient for this WhatsApp number. Only after the patient gave their full name and "
                    "date of birth and agreed to receive WhatsApp messages and to the privacy notice.",
     "input_schema": {"type": "object", "properties": {
         "full_name": {"type": "string"},
         "birth_date": {"type": "string", "description": "YYYY-MM-DD"},
         "consent": {"type": "boolean", "description": "true only if the patient explicitly agreed"}},
         "required": ["full_name", "birth_date", "consent"]}},
    {"name": "find_free_slots",
     "description": "Free start times for an appointment type, earliest first, spread over several days.",
     "input_schema": {"type": "object", "properties": {
         "type_key": {"type": "string", "description": "one of the appointment type keys"},
         "from_date": {"type": "string", "description": "YYYY-MM-DD, optional: first day to search"},
         "part_of_day": {"type": "string", "enum": ["any", "morning", "afternoon"]}},
         "required": ["type_key"]}},
    {"name": "book_appointment",
     "description": "Book one of the start times returned by find_free_slots, after the patient chose it.",
     "input_schema": {"type": "object", "properties": {
         "type_key": {"type": "string"}, "start": {"type": "string", "description": "ISO start time from find_free_slots"}},
         "required": ["type_key", "start"]}},
    {"name": "reschedule_appointment",
     "description": "Move one of this patient's upcoming appointments to a free start time.",
     "input_schema": {"type": "object", "properties": {
         "appointment_id": {"type": "string"}, "new_start": {"type": "string"}},
         "required": ["appointment_id", "new_start"]}},
    {"name": "cancel_appointment",
     "description": "Cancel one of this patient's upcoming appointments, after the patient confirmed which one.",
     "input_schema": {"type": "object", "properties": {"appointment_id": {"type": "string"}},
                      "required": ["appointment_id"]}},
    {"name": "confirm_appointment",
     "description": "Mark an upcoming appointment as confirmed (the patient said they will come).",
     "input_schema": {"type": "object", "properties": {"appointment_id": {"type": "string"}},
                      "required": ["appointment_id"]}},
    {"name": "add_to_waitlist",
     "description": "Put the patient on the waitlist for an appointment type; they get a message when a time frees up.",
     "input_schema": {"type": "object", "properties": {
         "type_key": {"type": "string"}, "notes": {"type": "string", "description": "preferred days or times"}},
         "required": ["type_key"]}},
    {"name": "handoff_to_staff",
     "description": "Pass the conversation to the clinic staff: for medical questions, treatment-plan visits, prices "
                    "for a specific case, complaints, anything you can't do, or when the patient asks for a person. "
                    "urgent=true for severe pain, swelling, bleeding, injury or fever.",
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string"}, "urgent": {"type": "boolean"}},
         "required": ["reason", "urgent"]}},
]


def system_prompt(cfg, now):
    c = cfg["clinic"]
    types = "\n".join(
        f"- {k}: {t['name_he']} ({t['minutes']} דק׳; קבוצה: {cfg['lines_he'][t['line']]}; "
        f"self_book={t['self_book']}{'; new patients may book' if t.get('new_patients') else ''}"
        f"{'; URGENT' if t.get('urgent') else ''})"
        for k, t in cfg["appointment_types"].items())
    hours = []
    names = {"sun": "ראשון", "mon": "שני", "tue": "שלישי", "wed": "רביעי", "thu": "חמישי", "fri": "שישי", "sat": "שבת"}
    for k, label in names.items():
        r = cfg["hours"].get(k) or []
        hours.append(f"יום {label}: {', '.join(r) if r else 'סגור'}")
    return f"""You are the WhatsApp booking assistant of {c['name_he']}. You help patients book, move and cancel
appointments and answer practical questions about the clinic.

Always write in Hebrew (unless the patient writes in another language), warmly and briefly, like a friendly
receptionist: short WhatsApp messages, no markdown headings or tables. Use the patient's first name once you know it.
Dates in Israeli style, e.g. {hebrew_label(now)}.

Now: {hebrew_label(now)} ({now.isoformat(timespec='minutes')}, Asia/Jerusalem).

The clinic
- Doctor: {c['doctor_he']}. Clinic phone: {c.get('phone') or '(not set)'}.
- Address: {c.get('address_he') or '(not set – say staff will send it)'}. Parking: {c.get('parking_he') or '(not set)'}.
- Opening hours: {'; '.join(hours)}. Closed on Jewish holidays.
- {c.get('notes_he') or ''}
- The clinic has one treatment chair: one patient at a time.

Appointment types (key: name, length):
{types}

How to work
1. Start every conversation with find_patient.
2. Ask what the visit is for if it isn't clear. Offer the service groups (בדיקות וטיפולי היגיינה / טיפולים משמרים /
   שיקום הפה / אסתטיקה / כאב או מקרה דחוף), then the type.
3. New patients (not registered, or existing_patient=false) may only book types marked "new patients may book":
   their first visit is always first_visit (an exam that also serves as a consultation), or urgent. Before booking,
   register them: ask for full name and date of birth, then ask explicitly: "האם את/ה מסכים/ה לקבל מאיתנו הודעות
   WhatsApp ולמדיניות הפרטיות של המרפאה?" Register only with a clear yes. Tell new patients the booking is
   pending until they confirm in the reminder message the day before.
4. self_book=yes: offer 2-3 times from find_free_slots and book the one the patient picks. Never invent times.
   self_book=plan (filling, root canal, whitening session): book only if the patient says it is part of a treatment
   plan Dr. already set; if unsure, hand off. self_book=no: hand off to staff.
5. Before cancelling or moving, say which appointment (type, day, time) and get a yes.
6. If nothing suitable is free, offer the waitlist.
7. Urgent symptoms (severe pain, swelling, bleeding, injury, fever): offer the earliest urgent slot, call
   handoff_to_staff with urgent=true, and give the clinic phone. If it sounds like an emergency (swelling of the face
   or throat, trouble breathing or swallowing, heavy bleeding, an accident), tell them to call 101 (מד״א) or go to
   the emergency room now.
8. Never diagnose, never give medical advice or medication advice, never quote a price for a specific case.
   Medical questions -> handoff_to_staff.
9. If a tool fails, apologise briefly and give the clinic phone.
"""


def _blocks(content):
    """Model response blocks as plain dicts to keep in the conversation history."""
    out = []
    for b in content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def _trim(messages):
    """Keep the last KEEP_MESSAGES messages, starting at a patient's message (never at a tool result)."""
    if len(messages) <= KEEP_MESSAGES:
        return messages
    cut = messages[-KEEP_MESSAGES:]
    while cut and not (cut[0]["role"] == "user" and isinstance(cut[0]["content"], str)):
        cut = cut[1:]
    return cut


class Tools:
    """The tools, bound to one WhatsApp number."""

    def __init__(self, phone, scheduler, store, notifier):
        self.phone, self.sch, self.store, self.notify = phone, scheduler, store, notifier

    def _appt(self, a):
        t = self.sch.cfg["appointment_types"].get(a.type_key, {})
        return {"appointment_id": a.id, "type_key": a.type_key, "type": t.get("name_he", a.type_key),
                "start": a.start.isoformat(), "label": hebrew_label(a.start), "status": a.status}

    def find_patient(self):
        p = self.store.patient(self.phone)
        return {"registered": bool(p), "name": p and p["full_name"],
                "existing_patient": bool(p and p["existing_patient"]),
                "upcoming_appointments": [self._appt(a) for a in self.sch.upcoming(self.phone)],
                "waitlist": [{"type_key": w["type_key"], "offered_a_slot": bool(w["offered_at"])}
                             for w in self.store.waitlist_for(self.phone)]}

    def register_patient(self, full_name, birth_date, consent):
        if not consent:
            return {"error": "the patient must agree to WhatsApp messages and the privacy notice first"}
        try:
            date.fromisoformat(birth_date)
        except ValueError:
            return {"error": "birth_date must be YYYY-MM-DD"}
        self.store.register_patient(self.phone, full_name.strip(), birth_date, consent)
        return {"ok": True}

    def find_free_slots(self, type_key, from_date=None, part_of_day="any"):
        t = self.sch.type(type_key)
        if t["self_book"] == "no":
            return {"error": "staff schedule this type; use handoff_to_staff"}
        fd = date.fromisoformat(from_date) if from_date else None
        slots = self.sch.find_slots(type_key, fd, part_of_day or "any")
        return {"type": t["name_he"], "minutes": t["minutes"], "slots": slots}

    def book_appointment(self, type_key, start):
        p = self.store.patient(self.phone)
        if not p or not p["consent_at"]:
            return {"error": "register the patient (name, birth date, consent) before booking"}
        t = self.sch.type(type_key)
        new = not p["existing_patient"]
        if new and not t.get("new_patients"):
            return {"error": "new patients can only book first_visit or urgent"}
        if t["self_book"] == "no":
            return {"error": "staff schedule this type; use handoff_to_staff"}
        a = self.sch.book(self.phone, p["full_name"], type_key, start, tentative=new and not t.get("urgent"))
        self.store.close_waitlist(self.phone)
        self.store.audit(self.phone, "book", f"{type_key} {a.start.isoformat()}")
        if t.get("urgent"):
            self.notify.staff(self.phone, f"תור דחוף נקבע: {hebrew_label(a.start)} – {p['full_name']}")
        return {"booked": self._appt(a), "pending_confirmation": a.status == "tentative"}

    def reschedule_appointment(self, appointment_id, new_start):
        old = self.sch.cal.get(appointment_id)
        a = self.sch.reschedule(self.phone, appointment_id, new_start)
        self.store.audit(self.phone, "reschedule", f"{appointment_id} -> {a.start.isoformat()}")
        if old:
            self.notify.slot_freed(old, exclude_phone=self.phone)
        return {"moved": self._appt(a)}

    def cancel_appointment(self, appointment_id):
        a = self.sch.cancel(self.phone, appointment_id)
        self.store.audit(self.phone, "cancel", f"{appointment_id} {a.start.isoformat()}")
        self.notify.slot_freed(a, exclude_phone=self.phone)
        return {"cancelled": self._appt(a)}

    def confirm_appointment(self, appointment_id):
        a = self.sch.confirm(self.phone, appointment_id)
        self.store.audit(self.phone, "confirm", appointment_id)
        return {"confirmed": self._appt(a)}

    def add_to_waitlist(self, type_key, notes=""):
        self.sch.type(type_key)
        self.store.add_waitlist(self.phone, type_key, notes)
        return {"ok": True}

    def handoff_to_staff(self, reason, urgent):
        self.store.add_handoff(self.phone, reason, urgent)
        self.notify.staff(self.phone, f"{'דחוף: ' if urgent else ''}{reason}")
        return {"ok": True, "clinic_phone": self.sch.cfg["clinic"].get("phone")}

    def run(self, name, args):
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if not fn:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except BookingError as e:
            return {"error": str(e)}
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}


class Assistant:
    def __init__(self, cfg, scheduler, store, notifier, client=None, model=None):
        self.cfg, self.sch, self.store, self.notify = cfg, scheduler, store, notifier
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or env("CLAUDE_MODEL", "claude-opus-5-5")

    def reply(self, phone, text, profile_name=None):
        messages, updated = self.store.history(phone)
        if updated and datetime.now(timezone.utc) - datetime.fromisoformat(updated) > FORGET_AFTER:
            messages = []
        note = f"[WhatsApp profile name: {profile_name}]\n" if profile_name and not messages else ""
        messages.append({"role": "user", "content": note + text})
        tools = Tools(phone, self.sch, self.store, self.notify)
        system = system_prompt(self.cfg, self.sch.now())
        answer = ""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(model=self.model, max_tokens=1024, system=system,
                                               tools=TOOLS, messages=messages)
            messages.append({"role": "assistant", "content": _blocks(resp.content)})
            answer = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason != "tool_use":
                break
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    out = tools.run(b.name, b.input or {})
                    log.info("tool %s %s -> %s", b.name, b.input, out)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, ensure_ascii=False, default=str)})
            messages.append({"role": "user", "content": results})
        self.store.save_history(phone, _trim(messages))
        phone_no = self.cfg["clinic"].get("phone")
        return answer or f"מצטערים, משהו השתבש. אפשר להתקשר למרפאה: {phone_no}"
