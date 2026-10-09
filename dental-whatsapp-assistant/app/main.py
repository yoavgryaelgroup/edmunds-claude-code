"""The web server: the WhatsApp webhook, and a demo chat page that works without WhatsApp.

Run:  uvicorn app.main:app --port 8000      then open http://localhost:8000/demo
"""
import logging
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from .agent import Assistant
from .calendars import make_calendar
from .config import ROOT, env, load_clinic
from .scheduling import Scheduler, hebrew_label
from .store import Store
from .whatsapp import Notifier, WhatsApp, incoming_messages, signature_ok

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clinic")


class Services:
    def __init__(self, cfg=None, calendar=None, store=None, client=None):
        self.cfg = cfg or load_clinic()
        self.calendar = calendar or make_calendar(self.cfg, ZoneInfo(self.cfg["timezone"]))
        self.scheduler = Scheduler(self.cfg, self.calendar)
        self.store = store or Store(env("DB_PATH", str(ROOT / "assistant.db")))
        self.whatsapp = WhatsApp()
        self.notifier = Notifier(self.cfg, self.whatsapp, self.store, self.scheduler)
        self.assistant = Assistant(self.cfg, self.scheduler, self.store, self.notifier, client=client)


def create_app(services=None):
    app = FastAPI(title="Dental clinic WhatsApp assistant")
    app.state.svc = services
    svc = lambda: app.state.svc or setattr(app.state, "svc", Services()) or app.state.svc  # noqa: E731

    def handle(message_id, phone, name, text):
        s = svc()
        if not s.store.first_time(message_id):
            return
        s.whatsapp.mark_read(message_id)
        try:
            answer = s.assistant.reply(phone, text, name)
        except Exception:
            log.exception("assistant failed for +%s", phone)
            answer = f"מצטערים, אירעה תקלה זמנית. אפשר להתקשר למרפאה: {s.cfg['clinic'].get('phone')}"
        s.whatsapp.send_text(phone, answer)

    @app.get("/health")
    def health():
        s = svc()
        return {"ok": True, "calendar": type(s.calendar).__name__, "whatsapp": s.whatsapp.configured}

    @app.get("/webhook")
    def verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == env("WHATSAPP_VERIFY_TOKEN"):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403)

    @app.post("/webhook")
    async def webhook(request: Request, tasks: BackgroundTasks):
        body = await request.body()
        if not signature_ok(env("WHATSAPP_APP_SECRET"), body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(403, "bad signature")
        for msg in incoming_messages(await request.json()):
            tasks.add_task(handle, *msg)
        return {"ok": True}       # answer Meta at once; the reply is sent from the background task

    # ------------------------------------------------------------------ demo (no WhatsApp needed)

    def demo_on():
        if env("DEMO_ENABLED", "1") == "0":   # set DEMO_ENABLED=0 on a public server
            raise HTTPException(404)

    class DemoMessage(BaseModel):
        phone: str
        text: str
        name: str | None = None

    @app.post("/demo/message")
    def demo_message(m: DemoMessage):
        demo_on()
        s = svc()
        phone = "".join(ch for ch in m.phone if ch.isdigit())
        before = len(s.notifier.sent)
        answer = s.assistant.reply(phone, m.text, m.name)
        return {"reply": answer, "notifications": s.notifier.sent[before:]}

    @app.get("/demo/appointments")
    def demo_appointments():
        demo_on()
        s = svc()
        now = s.scheduler.now()
        out = []
        for a in s.calendar.list_between(now - timedelta(hours=1), now + timedelta(days=60)):
            t = s.cfg["appointment_types"].get(a.type_key, {})
            out.append({"label": hebrew_label(a.start), "type": t.get("name_he", a.type_key), "name": a.name,
                        "phone": a.phone, "status": a.status})
        return {"calendar": type(s.calendar).__name__, "appointments": out, "handoffs": s.store.open_handoffs()}

    @app.post("/demo/run-reminders")
    def demo_run_reminders():
        demo_on()
        from . import reminders

        s = svc()
        before = len(s.notifier.sent)
        reminded, released = reminders.run(s)
        return {"reminded": reminded, "released": released, "notifications": s.notifier.sent[before:]}

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page():
        demo_on()
        return (Path(__file__).parent / "demo.html").read_text(encoding="utf-8")

    return app


app = create_app()
