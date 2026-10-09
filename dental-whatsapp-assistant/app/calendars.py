"""The clinic calendar: Google Calendar in production, an in-memory calendar for the demo and the tests.

Appointments made by the assistant are ordinary Google Calendar events. The patient's phone, the appointment type
and its status are kept in the event's private extended properties, so staff can also move or delete events by
hand in Google Calendar and the assistant sees the change.
"""
import itertools
from datetime import date, datetime, timedelta

from .scheduling import Appointment

SOURCE = "whatsapp-assistant"


class MemoryCalendar:
    """Keeps events in memory. `holiday_list` is [(date, name)], in the same naming as Google's calendar."""

    def __init__(self, holiday_list=None):
        self.events = {}
        self.blocked = []                 # (start, end) busy times not made by the assistant (staff events)
        self.holiday_list = holiday_list or []
        self._ids = itertools.count(1)

    def busy(self, start, end):
        out = [(a.start, a.end) for a in self.events.values() if a.start < end and a.end > start]
        out += [(s, e) for s, e in self.blocked if s < end and e > start]
        return sorted(out)

    def holidays(self, calendar_id, start, end):
        return [(d, n) for d, n in self.holiday_list if start <= d < end]

    def create(self, appt, type_name):
        appt.id = f"m{next(self._ids)}"
        self.events[appt.id] = appt
        return appt.id

    def get(self, appointment_id):
        a = self.events.get(appointment_id)
        return Appointment(**{**a.__dict__}) if a else None

    def update(self, appt, type_name):
        self.events[appt.id] = appt

    def delete(self, appointment_id):
        self.events.pop(appointment_id, None)

    def find_by_phone(self, phone, after):
        return sorted((Appointment(**{**a.__dict__}) for a in self.events.values()
                       if a.phone == phone and a.start >= after), key=lambda a: a.start)

    def list_between(self, start, end):
        return sorted((Appointment(**{**a.__dict__}) for a in self.events.values()
                       if start <= a.start < end), key=lambda a: a.start)


class GoogleCalendar:
    """Google Calendar through a service account that the clinic calendar is shared with ("Make changes to events")."""

    def __init__(self, calendar_id, credentials_file, tz):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=["https://www.googleapis.com/auth/calendar"])
        self.api = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.calendar_id, self.tz = calendar_id, tz

    # events <-> appointments
    def _to_appt(self, ev):
        p = (ev.get("extendedProperties") or {}).get("private") or {}
        return Appointment(
            id=ev["id"], start=datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(self.tz),
            end=datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(self.tz), type_key=p.get("type", ""),
            phone=p.get("phone", ""), name=p.get("name", ""), status=p.get("status", "confirmed"),
            reminder_sent=p.get("reminder_sent") == "1")

    def _body(self, appt, type_name):
        tentative = appt.status == "tentative"
        return {
            "summary": f"{'(ממתין לאישור) ' if tentative else ''}{type_name} – {appt.name}",
            "description": f"טלפון: +{appt.phone}\nנקבע דרך WhatsApp",
            "start": {"dateTime": appt.start.isoformat(), "timeZone": str(self.tz)},
            "end": {"dateTime": appt.end.isoformat(), "timeZone": str(self.tz)},
            "colorId": "5" if tentative else "10",   # yellow while waiting for confirmation, green when confirmed
            "extendedProperties": {"private": {
                "source": SOURCE, "phone": appt.phone, "name": appt.name, "type": appt.type_key,
                "status": appt.status, "reminder_sent": "1" if appt.reminder_sent else "0"}},
        }

    def busy(self, start, end):
        r = self.api.freebusy().query(body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": str(self.tz),
            "items": [{"id": self.calendar_id}]}).execute()
        return sorted((datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(self.tz),
                       datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(self.tz))
                      for b in r["calendars"][self.calendar_id]["busy"])

    def holidays(self, calendar_id, start, end):
        r = self.api.events().list(calendarId=calendar_id, timeMin=f"{start.isoformat()}T00:00:00Z",
                                   timeMax=f"{end.isoformat()}T00:00:00Z", singleEvents=True).execute()
        out = []
        for ev in r.get("items", []):
            d = ev["start"].get("date")
            if d:
                out.append((date.fromisoformat(d), ev.get("summary", "")))
        return out

    def create(self, appt, type_name):
        ev = self.api.events().insert(calendarId=self.calendar_id, body=self._body(appt, type_name)).execute()
        return ev["id"]

    def get(self, appointment_id):
        try:
            ev = self.api.events().get(calendarId=self.calendar_id, eventId=appointment_id).execute()
        except Exception:
            return None
        if ev.get("status") == "cancelled" or "dateTime" not in ev.get("start", {}):
            return None
        return self._to_appt(ev)

    def update(self, appt, type_name):
        self.api.events().patch(calendarId=self.calendar_id, eventId=appt.id,
                                body=self._body(appt, type_name)).execute()

    def delete(self, appointment_id):
        self.api.events().delete(calendarId=self.calendar_id, eventId=appointment_id).execute()

    def _list(self, start, end, **filters):
        items, token = [], None
        while True:
            args = dict(calendarId=self.calendar_id, timeMin=start.isoformat(), singleEvents=True,
                        orderBy="startTime", **filters)
            if end:
                args["timeMax"] = end.isoformat()
            if token:
                args["pageToken"] = token
            r = self.api.events().list(**args).execute()
            items += [e for e in r.get("items", []) if "dateTime" in e.get("start", {})]
            token = r.get("nextPageToken")
            if not token:
                return [self._to_appt(e) for e in items]

    def find_by_phone(self, phone, after):
        return self._list(after, None, privateExtendedProperty=f"phone={phone}")

    def list_between(self, start, end):
        return self._list(start, end, privateExtendedProperty=f"source={SOURCE}")


def make_calendar(cfg, tz):
    """Google Calendar when GOOGLE_CALENDAR_ID and GOOGLE_CREDENTIALS_FILE are set, else the in-memory demo."""
    from .config import env

    if env("GOOGLE_CALENDAR_ID") and env("GOOGLE_CREDENTIALS_FILE"):
        return GoogleCalendar(env("GOOGLE_CALENDAR_ID"), env("GOOGLE_CREDENTIALS_FILE"), tz)
    return MemoryCalendar()
