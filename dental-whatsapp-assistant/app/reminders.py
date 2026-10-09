"""Hourly job: reminders 24 h ahead, and release of new patients' bookings that were never confirmed.

Run every hour (cron, or Windows Task Scheduler):   python -m app.reminders
"""
import logging
from datetime import timedelta

from .scheduling import hebrew_label

log = logging.getLogger(__name__)


def run(svc):
    s, cfg = svc.scheduler, svc.cfg
    now = s.now()
    lead = timedelta(hours=cfg["scheduling"].get("reminder_hours_before", 24))
    deadline = timedelta(hours=cfg["scheduling"].get("confirm_deadline_hours", 12))
    tpl = cfg.get("whatsapp_templates") or {}
    reminded = released = 0
    for a in svc.calendar.list_between(now, now + lead + timedelta(hours=1)):
        t = cfg["appointment_types"].get(a.type_key, {})
        if a.status == "tentative" and a.start - now <= deadline:
            svc.calendar.delete(a.id)                      # never confirmed: free the chair for someone else
            svc.store.audit(a.phone, "released_unconfirmed", a.start.isoformat())
            svc.notifier.slot_freed(a, exclude_phone=a.phone)
            released += 1
            continue
        if not a.reminder_sent and a.start - now <= lead:
            first = a.name.split()[0] if a.name else ""
            svc.notifier.sent.append({"to": a.phone, "template": "reminder", "params": [first, t.get("name_he"),
                                                                                       hebrew_label(a.start)]})
            if tpl.get("reminder"):
                svc.whatsapp.send_template(a.phone, tpl["reminder"], tpl.get("language", "he"),
                                           [first, t.get("name_he", ""), hebrew_label(a.start)])
            a.reminder_sent = True
            svc.calendar.update(a, t.get("name_he", a.type_key))
            reminded += 1
    log.info("reminders sent: %d, unconfirmed bookings released: %d", reminded, released)
    return reminded, released


if __name__ == "__main__":
    from .main import Services

    logging.basicConfig(level=logging.INFO)
    run(Services(client=object()))   # the job never calls the AI model
