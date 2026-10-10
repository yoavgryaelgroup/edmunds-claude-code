"""Free slots, booking, rescheduling and cancelling against the clinic calendar.

The AI never writes to the calendar itself: it calls these methods, and every write re-checks under a lock that the
slot is still free, so two patients chatting at once can't get the same time.
"""
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import DAY_KEYS, parse_hm, parse_range

log = logging.getLogger(__name__)

HEB_DAYS = ["ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "שבת", "א׳"]  # by weekday(): Monday = ב׳ ... Sunday = א׳


EN_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def hebrew_label(dt):
    day = "שבת" if dt.weekday() == 5 else f"יום {HEB_DAYS[dt.weekday()]}"
    return f"{day} {dt.day}.{dt.month} בשעה {dt:%H:%M}"


def english_label(dt):
    return f"{EN_DAYS[dt.weekday()]} {dt.day}.{dt.month} at {dt:%H:%M}"


def label(dt, lang="he"):
    """A date and time the way a patient reads it, in their language."""
    return english_label(dt) if lang == "en" else hebrew_label(dt)


def type_name(t, lang="he"):
    return t.get(f"name_{lang}") or t["name_he"]


@dataclass
class Appointment:
    start: datetime
    end: datetime
    type_key: str
    phone: str
    name: str
    status: str = "confirmed"          # confirmed | tentative
    reminder_sent: bool = False
    id: str = ""
    extra: dict = field(default_factory=dict)


class BookingError(Exception):
    """A booking request that can't be done; the message is shown to the AI, which explains it to the patient."""


class Scheduler:
    def __init__(self, cfg, calendar, now=None):
        self.cfg, self.cal = cfg, calendar
        self.s = cfg["scheduling"]
        self.tz = ZoneInfo(cfg["timezone"])
        self._now = now or (lambda: datetime.now(self.tz))
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def now(self):
        return self._now()

    def type(self, key):
        t = self.cfg["appointment_types"].get(key)
        if not t:
            raise BookingError(f"unknown appointment type {key!r}")
        return t

    def _notice(self, t):
        return timedelta(minutes=t.get("min_notice_minutes", self.s.get("min_notice_minutes", 0)))

    def _at(self, d, t):
        return datetime.combine(d, t, tzinfo=self.tz)

    def _closures(self, start, end):
        """Dates closed all day, and eves (date -> closing time), from closed_dates and the holiday calendar."""
        closed = {date.fromisoformat(d) for d in self.s.get("closed_dates") or []}
        eves = {}
        h = self.s.get("holidays") or {}
        if h.get("calendar_id"):
            keywords = [k.lower() for k in h.get("closed_keywords") or []]
            try:
                holidays = self.cal.holidays(h["calendar_id"], start, end)
            except Exception:   # the holiday calendar is a convenience: never let it stop bookings
                log.exception("could not read the holiday calendar %s; add closures to closed_dates", h["calendar_id"])
                holidays = []
            for d, summary in holidays:
                name = summary.lower()
                if name.startswith("erev "):
                    if any(k in name for k in keywords):
                        eves[d] = parse_hm(h.get("eve_closes_at", "12:00"))
                elif any(k in name for k in keywords):
                    closed.add(d)
        return closed, eves

    def opening_windows(self, d, closed, eves):
        if d in closed:
            return []
        out = []
        for r in self.cfg["hours"].get(DAY_KEYS[d.weekday()]) or []:
            a, b = parse_range(r)
            if d in eves:
                b = min(b, eves[d])
            if a < b:
                out.append((self._at(d, a), self._at(d, b)))
        return out

    def urgent_holds(self, d, now):
        """Urgent windows still held on day d (each is released release_minutes_before its start)."""
        hold = self.s.get("urgent_hold") or {}
        release = timedelta(minutes=hold.get("release_minutes_before", 120))
        out = []
        for r in (hold.get("windows") or {}).get(DAY_KEYS[d.weekday()]) or []:
            a, b = parse_range(r)
            ws, we = self._at(d, a), self._at(d, b)
            if now < ws - release:
                out.append((ws, we))
        return out

    def _fits(self, t, start, busy, windows, holds):
        """True if an appointment of type t can start at `start`."""
        dur = timedelta(minutes=t["minutes"])
        buf = timedelta(minutes=self.s.get("buffer_minutes", 0))
        end = start + dur
        if not any(ws <= start and end <= we for ws, we in windows):
            return False
        if t.get("only_between"):
            ok = False
            for r in t["only_between"]:
                a, b = parse_range(r)
                if a <= start.time() < b:
                    ok = True
            if not ok:
                return False
        for bs, be in busy:  # existing appointments, each followed by the buffer; ours needs its buffer too
            if start < be + buf and bs < end + buf:
                return False
        if not t.get("urgent"):
            for hs, he in holds:
                if start < he and hs < end:
                    return False
        return True

    def _day_context(self, d, busy_all, closed, eves, now):
        windows = self.opening_windows(d, closed, eves)
        holds = self.urgent_holds(d, now)
        day_start, day_end = self._at(d, time(0)), self._at(d, time(0)) + timedelta(days=1)
        busy = [(bs, be) for bs, be in busy_all if bs < day_end and be > day_start]
        return windows, busy, holds

    # ------------------------------------------------------------------ queries

    def find_slots(self, type_key, from_date=None, part_of_day="any", limit=6):
        t = self.type(type_key)
        now = self.now()
        earliest = now + self._notice(t)
        latest = now + timedelta(days=self.s.get("max_days_ahead", 60))
        step = timedelta(minutes=self.s.get("slot_step_minutes", 15))
        per_day = self.s.get("offer_per_day", 2)
        d = max(earliest.date(), from_date or earliest.date())
        found = []
        while d <= latest.date() and len(found) < limit:
            chunk_end = min(d + timedelta(days=14), latest.date() + timedelta(days=1))
            a, b = self._at(d, time(0)), self._at(chunk_end, time(0))
            busy_all = self.cal.busy(a, b)
            closed, eves = self._closures(d, chunk_end)
            while d < chunk_end and len(found) < limit:
                windows, busy, holds = self._day_context(d, busy_all, closed, eves, now)
                today = 0
                for ws, we in windows:
                    c = ws
                    while c < we and today < per_day and len(found) < limit:
                        part_ok = part_of_day == "any" or (part_of_day == "morning") == (c.hour < 12)
                        if earliest <= c <= latest and part_ok and self._fits(t, c, busy, windows, holds):
                            found.append(c)
                            today += 1
                            # next offer after this one and its buffer, back on the grid
                            nxt = c + timedelta(minutes=t["minutes"] + self.s.get("buffer_minutes", 0))
                            c = ws + step * -(-(nxt - ws) // step)
                            continue
                        c += step
                d += timedelta(days=1)
        return [{"start": s.isoformat(), "label": hebrew_label(s), "label_en": english_label(s)} for s in found]

    def is_free(self, type_key, start, ignore_id=None):
        t = self.type(type_key)
        now = self.now()
        if start < now + self._notice(t):
            return False
        if start > now + timedelta(days=self.s.get("max_days_ahead", 60)):
            return False
        d = start.date()
        a = self._at(d, time(0))
        busy_all = self.cal.busy(a, a + timedelta(days=1))
        if ignore_id:
            own = self.cal.get(ignore_id)
            if own:
                busy_all = [(bs, be) for bs, be in busy_all if not (bs == own.start and be == own.end)]
        closed, eves = self._closures(d, d + timedelta(days=1))
        windows, busy, holds = self._day_context(d, busy_all, closed, eves, now)
        return self._fits(t, start, busy, windows, holds)

    def upcoming(self, phone):
        return self.cal.find_by_phone(phone, self.now())

    # ------------------------------------------------------------------ writes

    def _parse_start(self, start):
        dt = datetime.fromisoformat(start) if isinstance(start, str) else start
        return dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt.astimezone(self.tz)

    def book(self, phone, name, type_key, start, tentative=False):
        t = self.type(type_key)
        start = self._parse_start(start)
        with self._lock:
            if not self.is_free(type_key, start):
                raise BookingError("this time is no longer free; offer other times")
            appt = Appointment(start=start, end=start + timedelta(minutes=t["minutes"]), type_key=type_key,
                               phone=phone, name=name, status="tentative" if tentative else "confirmed")
            appt.id = self.cal.create(appt, t["name_he"])
        return appt

    def _own(self, appointment_id, phone):
        appt = self.cal.get(appointment_id)
        if not appt or appt.phone != phone:
            raise BookingError("no such appointment for this patient")
        if appt.start < self.now():
            raise BookingError("this appointment is in the past")
        return appt

    def reschedule(self, phone, appointment_id, new_start):
        appt = self._own(appointment_id, phone)
        t = self.type(appt.type_key)
        new_start = self._parse_start(new_start)
        with self._lock:
            if not self.is_free(appt.type_key, new_start, ignore_id=appointment_id):
                raise BookingError("this time is not free; offer other times")
            appt.start, appt.end = new_start, new_start + timedelta(minutes=t["minutes"])
            appt.reminder_sent = False
            self.cal.update(appt, t["name_he"])
        return appt

    def cancel(self, phone, appointment_id):
        appt = self._own(appointment_id, phone)
        self.cal.delete(appointment_id)
        return appt

    def confirm(self, phone, appointment_id):
        appt = self._own(appointment_id, phone)
        appt.status = "confirmed"
        self.cal.update(appt, self.type(appt.type_key)["name_he"])
        return appt
