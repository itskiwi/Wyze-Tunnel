"""Morning summary: one alert in the morning with what the cameras saw overnight."""

from collections import Counter
from datetime import datetime, timedelta

from wyzebridge.webhooks import ALERTS, send_webhook

_sent_on = {"day": None}


def window(now: datetime) -> tuple[datetime, datetime]:
    """Last night, from the quiet hours if they're set, else 22:00 to 07:00."""
    start = datetime.strptime(ALERTS.get("quiet_start") or "22:00", "%H:%M").time()
    end = datetime.strptime(ALERTS.get("quiet_end") or "07:00", "%H:%M").time()
    until = datetime.combine(now.date(), end)
    since = datetime.combine(now.date() - timedelta(days=1) if start > end else now.date(), start)
    return since, until


def compose(events: list[dict], since: datetime, until: datetime) -> str:
    night = [e for e in events if since.timestamp() <= e["ts"] < until.timestamp()]
    span = f"{since:%H:%M} to {until:%H:%M}"
    if not night:
        return f"A quiet night. Nothing was seen from {span}."
    rings = sum(1 for e in night if e.get("label") == "rang the doorbell")
    seen = Counter(e["name"] for e in night if e.get("label") != "rang the doorbell")
    parts = []
    if seen:
        total = sum(seen.values())
        where = ", ".join(f"{n} on {name}" for name, n in seen.most_common(3))
        parts.append(f"motion {total} time{'s'[:total ^ 1]} ({where})")
    if rings:
        parts.append("the doorbell rang " + ("once" if rings == 1 else f"{rings} times"))
    return f"From {span}: " + " and ".join(parts) + "."


def check(events: list[dict], now: datetime | None = None) -> bool:
    """Send the summary once a day, at the end of the night window."""
    now = now or datetime.now()
    if not ALERTS.get("summary") or _sent_on["day"] == now.date():
        return False
    since, until = window(now)
    if not until <= now < until + timedelta(hours=2):  # a restart later in the day doesn't resend it
        return False
    _sent_on["day"] = now.date()
    # shortcut: reads the in-memory event list (last 100 events); persist events if busy nights overflow it.
    return send_webhook("summary", "all", compose(events, since, until))
