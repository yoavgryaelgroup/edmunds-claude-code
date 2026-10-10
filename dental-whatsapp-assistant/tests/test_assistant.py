import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import reminders
from app.agent import Assistant, Tools
from app.calendars import MemoryCalendar
from app.config import load_clinic
from app.main import Services, create_app
from app.scheduling import BookingError, Scheduler
from app.store import Store
from app.whatsapp import Notifier, WhatsApp, incoming_messages, signature_ok

TZ = ZoneInfo("Asia/Jerusalem")
SUNDAY = datetime(2026, 10, 11, 10, 0, tzinfo=TZ)   # a Sunday: the clinic opens 14:00-18:00


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


@pytest.fixture
def clock():
    return {"now": SUNDAY}


@pytest.fixture
def svc(clock):
    cfg = load_clinic()
    cal = MemoryCalendar()
    s = Services(cfg=cfg, calendar=cal, store=Store(), client=object())
    s.scheduler._now = lambda: clock["now"]
    return s


def starts(slots):
    return [datetime.fromisoformat(x["start"]) for x in slots]


# ---------------------------------------------------------------- scheduling

def test_slots_follow_opening_hours_and_spread_over_days(svc):
    got = starts(svc.scheduler.find_slots("cleaning", limit=6))
    assert got[:2] == [at(11, 14), at(11, 15)]           # 45 min + 10 min buffer, back on the 15-min grid
    assert got[2:4] == [at(12, 14), at(12, 15)]          # Monday
    assert got[4] == at(14, 9)                           # Tuesday is closed; Wednesday opens at 9:00
    assert all(s.weekday() not in (1, 5) for s in got)   # never Tuesday or Saturday


def test_buffer_after_each_appointment(svc):
    svc.scheduler.book("972500000001", "דנה", "cleaning", at(11, 14))       # 14:00-14:45
    assert not svc.scheduler.is_free("checkup", at(11, 14, 45))            # still cleaning the room
    assert svc.scheduler.is_free("checkup", at(11, 14, 55))
    with pytest.raises(BookingError):
        svc.scheduler.book("972500000002", "יוסי", "checkup", at(11, 14, 30))


def test_urgent_slot_is_held_then_released(svc, clock):
    assert not svc.scheduler.is_free("checkup", at(11, 16))     # held for urgent cases
    assert svc.scheduler.is_free("urgent", at(11, 16))
    assert svc.scheduler.is_free("urgent", at(11, 10, 30)) is False and svc.scheduler.find_slots("urgent")
    clock["now"] = at(11, 13, 5)                                  # less than 3 h before: released
    assert svc.scheduler.is_free("checkup", at(11, 16))
    clock["now"] = at(11, 15, 40)                                 # urgent cases may come in 30 min ahead
    assert svc.scheduler.is_free("urgent", at(11, 16, 15)) and not svc.scheduler.is_free("checkup", at(11, 16, 15))


def test_min_notice_and_long_treatments_at_session_start(svc):
    assert not svc.scheduler.is_free("checkup", at(11, 11))       # before opening
    rc = starts(svc.scheduler.find_slots("root_canal", limit=4))
    assert all(s.hour == 14 or s.hour == 9 or s.hour == 8 for s in rc)


def test_holidays_close_the_clinic(svc):
    svc.calendar.holiday_list = [(date(2026, 10, 12), "Erev Yom Kippur"), (date(2026, 10, 14), "Yom Kippur")]
    got = starts(svc.scheduler.find_slots("checkup", from_date=date(2026, 10, 12), limit=4))
    assert all(s.date() not in (date(2026, 10, 12), date(2026, 10, 14)) for s in got)   # eve closes at 12:00


def test_reschedule_and_cancel_only_own_appointments(svc):
    a = svc.scheduler.book("972500000001", "דנה", "checkup", at(11, 14))
    with pytest.raises(BookingError):
        svc.scheduler.cancel("972599999999", a.id)
    moved = svc.scheduler.reschedule("972500000001", a.id, at(11, 14, 15))   # overlaps its own old time: fine
    assert moved.start == at(11, 14, 15)
    svc.scheduler.cancel("972500000001", a.id)
    assert svc.scheduler.upcoming("972500000001") == []


# ---------------------------------------------------------------- tools

def tools(svc, phone="972500000001"):
    return Tools(phone, svc.scheduler, svc.store, svc.notifier)


def test_new_patient_must_register_and_book_first_visit(svc):
    t = tools(svc)
    assert "error" in t.run("book_appointment", {"type_key": "first_visit", "start": at(11, 14).isoformat()})
    assert "error" in t.run("register_patient", {"full_name": "דנה כהן", "birth_date": "1990-05-01", "consent": False})
    assert t.run("register_patient", {"full_name": "דנה כהן", "birth_date": "1990-05-01", "consent": True})["ok"]
    assert "error" in t.run("book_appointment", {"type_key": "cleaning", "start": at(11, 14).isoformat()})
    r = t.run("book_appointment", {"type_key": "first_visit", "start": at(11, 14).isoformat()})
    assert r["pending_confirmation"] is True
    info = t.run("find_patient", {})
    assert info["upcoming_appointments"][0]["status"] == "tentative"
    t.run("confirm_appointment", {"appointment_id": r["booked"]["appointment_id"]})
    assert t.run("find_patient", {})["upcoming_appointments"][0]["status"] == "confirmed"


def test_existing_patient_books_directly_and_waitlist_gets_offer(svc):
    svc.store.register_patient("972500000001", "דנה כהן", "1990-05-01", True)
    svc.store.set_existing("972500000001")
    svc.store.register_patient("972500000002", "יוסי לוי", "1985-01-01", True)
    t1, t2 = tools(svc), tools(svc, "972500000002")
    r = t1.run("book_appointment", {"type_key": "cleaning", "start": at(11, 14).isoformat()})
    assert r["pending_confirmation"] is False
    t2.run("add_to_waitlist", {"type_key": "checkup", "notes": "ימי ראשון"})
    t1.run("cancel_appointment", {"appointment_id": r["booked"]["appointment_id"]})
    offer = [n for n in svc.notifier.sent if n["template"] == "waitlist_offer"]
    assert offer and offer[0]["to"] == "972500000002"


def test_self_book_no_and_urgent_handoff(svc):
    t = tools(svc)
    assert "error" in t.run("find_free_slots", {"type_key": "restorative_treatment"})
    assert t.run("handoff_to_staff", {"reason": "כאב חזק ונפיחות", "urgent": True})["clinic_phone"] == "051-564-6322"
    assert svc.store.open_handoffs()[0]["urgent"] == 1


# ---------------------------------------------------------------- the AI loop, with a fake model

class FakeClient:
    """Plays a short script: look up the patient, then answer."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", content=[
                SimpleNamespace(type="tool_use", id="t1", name="find_patient", input={})])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="שלום! במה אפשר לעזור?")])


def test_assistant_runs_tools_and_keeps_history(svc):
    fake = FakeClient()
    a = Assistant(svc.cfg, svc.scheduler, svc.store, svc.notifier, client=fake, model="test")
    assert a.reply("972500000001", "היי", "Dana") == "שלום! במה אפשר לעזור?"
    sent = fake.calls[1]["messages"]
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert json.loads(sent[2]["content"][0]["content"])["registered"] is False
    assert "יום א׳" in fake.calls[0]["system"]
    history, _ = svc.store.history("972500000001")
    assert history[0]["content"].endswith("היי") and len(history) == 4


# ---------------------------------------------------------------- WhatsApp webhook and demo

def test_webhook_parsing_and_signature():
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "972500000001", "profile": {"name": "Dana"}}],
        "messages": [{"id": "wamid.1", "from": "972500000001", "type": "text", "text": {"body": "שלום"}},
                     {"id": "wamid.2", "from": "972500000001", "type": "button", "button": {"text": "מאשר/ת"}}]}}]}]}
    assert incoming_messages(payload) == [("wamid.1", "972500000001", "Dana", "שלום"),
                                          ("wamid.2", "972500000001", "Dana", "מאשר/ת")]
    body = json.dumps(payload).encode()
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert signature_ok("secret", body, good) and not signature_ok("secret", body, "sha256=00")


def test_demo_endpoints(svc):
    svc.assistant.client = FakeClient()
    client = TestClient(create_app(svc))
    r = client.post("/demo/message", json={"phone": "+972 50-000-0001", "text": "היי"})
    assert r.status_code == 200 and r.json()["reply"]
    assert client.get("/demo/appointments").json()["calendar"] == "MemoryCalendar"
    assert "עוזר WhatsApp" in client.get("/demo").text


def test_webhook_verification(svc, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "tok")
    client = TestClient(create_app(svc))
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"})
    assert r.text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x"}).status_code == 403


# ---------------------------------------------------------------- hourly job

def test_reminders_and_release_of_unconfirmed(svc, clock):
    keep = svc.scheduler.book("972500000001", "דנה כהן", "checkup", at(12, 14))           # Monday 14:00
    drop = svc.scheduler.book("972500000002", "יוסי לוי", "first_visit", at(12, 15), tentative=True)
    clock["now"] = at(11, 14, 30)                       # 23.5 h before keep, 24.5 h before drop
    assert reminders.run(svc) == (1, 0)
    assert svc.calendar.get(keep.id).reminder_sent
    clock["now"] = at(11, 15, 0)                        # next hourly run: drop is now 24 h ahead
    assert reminders.run(svc) == (1, 0)
    clock["now"] = at(12, 3, 30)                        # 11.5 h before drop, still not confirmed
    assert reminders.run(svc) == (0, 1)
    assert svc.calendar.get(drop.id) is None


def test_unreadable_holiday_calendar_does_not_stop_bookings(svc):
    def broken(*a):
        raise RuntimeError("403 from Google")
    svc.calendar.holidays = broken
    assert svc.scheduler.find_slots("checkup", limit=1)
