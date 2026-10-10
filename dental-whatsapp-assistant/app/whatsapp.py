"""WhatsApp Business Platform (Meta Cloud API): sending messages, reading webhooks, checking their signature."""
import hashlib
import hmac
import logging

import httpx

from .config import env
from .scheduling import label, type_name

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"


class WhatsApp:
    def __init__(self, token=None, phone_number_id=None):
        self.token = token or env("WHATSAPP_TOKEN")
        self.phone_number_id = phone_number_id or env("WHATSAPP_PHONE_NUMBER_ID")

    @property
    def configured(self):
        return bool(self.token and self.phone_number_id)

    def _post(self, body):
        if not self.configured:
            log.info("WhatsApp not configured; would send %s", body)
            return None
        r = httpx.post(f"{GRAPH}/{self.phone_number_id}/messages", json={"messaging_product": "whatsapp", **body},
                       headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
        if r.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text)
        return r

    def send_text(self, to, text):
        return self._post({"to": to, "type": "text", "text": {"body": text[:4096]}})

    def send_template(self, to, name, language, params):
        return self._post({"to": to, "type": "template", "template": {
            "name": name, "language": {"code": language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}]}})

    def mark_read(self, message_id):
        return self._post({"status": "read", "message_id": message_id})


def signature_ok(app_secret, body, header):
    """Meta signs every webhook with the app secret (X-Hub-Signature-256: sha256=<hex>)."""
    if not app_secret:
        return True
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def incoming_messages(payload):
    """(message id, sender phone, profile name, text) for each patient message in a webhook payload."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind = m.get("type")
                if kind == "text":
                    text = m["text"]["body"]
                elif kind == "button":                                   # quick-reply button on a template
                    text = m["button"].get("text") or m["button"].get("payload", "")
                elif kind == "interactive":
                    i = m["interactive"]
                    text = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                else:
                    text = f"[the patient sent a {kind} message, which the assistant cannot read]"
                out.append((m["id"], m["from"], names.get(m["from"]), text))
    return out


class Notifier:
    """Messages the clinic starts: staff alerts and waitlist offers (need approved templates outside the 24 h window)."""

    def __init__(self, cfg, whatsapp, store, scheduler):
        self.cfg, self.wa, self.store, self.sch = cfg, whatsapp, store, scheduler
        self.tpl = cfg.get("whatsapp_templates") or {}
        self.sent = []   # what was sent, for the demo page and the tests

    def template(self, to, key, params, lang="he"):
        self.sent.append({"to": to, "template": key, "params": params, "language": lang})
        if self.tpl.get(key):
            codes = self.tpl.get("languages") or {}
            self.wa.send_template(to, self.tpl[key], codes.get(lang, self.tpl.get("language", lang)), params)

    def staff(self, patient_phone, reason):
        for number in self.cfg.get("staff_alert_numbers") or []:
            self.template(number, "staff_alert", [f"+{patient_phone}", reason])
        if not self.cfg.get("staff_alert_numbers"):
            self.sent.append({"to": "staff", "template": "staff_alert", "params": [f"+{patient_phone}", reason]})
        log.warning("STAFF ALERT from +%s: %s", patient_phone, reason)

    def slot_freed(self, appt, exclude_phone=None):
        """Offer a freed time to the first patient waiting for the same type (or a shorter one that fits)."""
        types = self.cfg["appointment_types"]
        minutes = (appt.end - appt.start).total_seconds() / 60
        fitting = [k for k, t in types.items() if t["minutes"] <= minutes and t["self_book"] != "no"]
        w = self.store.next_waiting(fitting, exclude_phone) if fitting else None
        if not w:
            return None
        self.store.mark_offered(w["id"])
        lang = self.store.language(w["phone"], self.cfg.get("default_language", "he"))
        self.template(w["phone"], "waitlist_offer", [type_name(types[w["type_key"]], lang), label(appt.start, lang)],
                       lang)
        self.store.audit(w["phone"], "waitlist_offer", appt.start.isoformat())
        return w
