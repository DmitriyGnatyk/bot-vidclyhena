"""Сторінка графіка відключень (/view).

Її відкриває screenshot-api.org і знімає елемент #schedule — це і є картинка в Telegram.
Pillow більше не використовується: якщо API недоступний, бот шле текстову версію.

Формат даних (days) — список днів:
    [{"date": "19.10.2026", "slots": [["1.1", "08:00-12:00"], ["1.2", ""], ...]}, ...]
Порожній рядок = відключень немає.

Подивитись дизайн без бота й токена:
    python render.py        → створить preview.html, відкрий його в браузері
"""
from __future__ import annotations

import html
import random
import re
from datetime import date, datetime, timedelta

MAX_DAYS = 3  # більше днів — картинка стає занадто високою для Telegram

WEEKDAYS = ["Понеділок", "Вівторок", "Середа", "Четвер", "П’ятниця", "Субота", "Неділя"]
MONTHS = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]

# ───────────────────────── Парсинг ─────────────────────────
_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")


def parse_intervals(text: str) -> list[tuple[int, int]]:
    """'00:00-04:00, 22:00-24:00' → [(0, 240), (1320, 1440)] (хвилини від початку доби)."""
    out: list[tuple[int, int]] = []
    for h1, m1, h2, m2 in _RANGE.findall(text or ""):
        a, b = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        a, b = min(a, 1440), min(b, 1440)
        if b > a:
            out.append((a, b))
        elif b < a:  # через північ: 22:00-02:00
            out += [(a, 1440), (0, b)]
    return out


def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def interval_labels(raw: str, intervals: list[tuple[int, int]]) -> list[str]:
    """['08:00–12:00', '18:00–22:00']; якщо формат незрозумілий — сирий текст; якщо порожньо — []."""
    if intervals:
        return [f"{fmt_min(a)}–{fmt_min(b)}" for a, b in intervals]
    raw = (raw or "").strip()
    return [raw] if raw and raw != "—" else []


def parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip()[:10], "%d.%m.%Y").date()
    except ValueError:
        return None


def weekday_name(text: str) -> str:
    d = parse_date(text)
    return WEEKDAYS[d.weekday()] if d else ""


# ───────────────────────── Тестові дані ─────────────────────────
def demo_days(seed: int, today: date) -> list[dict]:
    """Тестові дані. Однаковий seed → однакові дані."""
    days = []
    for d in range(2):
        rnd = random.Random(seed * 10 + d)
        slots = []
        for q in range(1, 7):
            for s in (1, 2):
                ranges = []
                if rnd.random() > 0.15:
                    t = rnd.randrange(0, 10)
                    for _ in range(rnd.choice([1, 1, 2])):
                        end = min(t + rnd.choice([2, 3, 4]), 24)
                        ranges.append(f"{t:02d}:00-{end:02d}:00")
                        t = end + rnd.randrange(2, 6)
                        if t >= 22:
                            break
                slots.append([f"{q}.{s}", ", ".join(ranges)])
        days.append({"date": (today + timedelta(days=d)).strftime("%d.%m.%Y"), "slots": slots})
    return days


# ───────────────────────── Дизайн ─────────────────────────
CSS = """
:root{
  --bg:#0b1020; --card:#131a2e; --line:rgba(255,255,255,.07);
  --text:#f3f6ff; --muted:#8d99b8;
  --on:rgba(52,211,153,.20); --on-text:#34d399;
  --off1:#ff6b81; --off2:#e11d48;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg)}
body{
  font-family:Inter,"Segoe UI",Roboto,"Helvetica Neue","DejaVu Sans",Arial,sans-serif;
  color:var(--text);-webkit-font-smoothing:antialiased;padding:24px 0;
}
#schedule{
  width:720px;margin:0 auto;padding:28px 24px 22px;
  background:linear-gradient(180deg,#101a3a 0,var(--bg) 280px);
}
.top{display:flex;align-items:center;gap:14px;margin-bottom:20px}
.logo{width:50px;height:50px;border-radius:15px;display:grid;place-items:center;
  background:linear-gradient(135deg,#fbbf24,#f97316)}
.top h1{font-size:27px;font-weight:800;letter-spacing:-.01em;line-height:1.1}
.top p{color:var(--muted);font-size:15px;margin-top:4px}
.stamp{margin-left:auto;text-align:right}
.stamp small{display:block;color:var(--muted);font-size:13px}
.stamp strong{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}

.day{background:var(--card);border:1px solid var(--line);border-radius:22px;
  padding:20px 22px 14px;margin-bottom:16px}
.dh{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.dh h2{font-size:23px;font-weight:700}
.dh .dt{color:var(--muted);font-size:17px}
.pill{font-size:12px;font-weight:800;padding:4px 10px;border-radius:99px;
  background:rgba(251,191,36,.16);color:#fbbf24;text-transform:uppercase;letter-spacing:.05em}
.dh .cnt{margin-left:auto;color:var(--muted);font-size:14px}

.row{display:flex;align-items:center;gap:12px;min-height:38px;padding:3px 0}
.row.g{margin-top:7px;padding-top:10px;border-top:1px solid var(--line)}
.q{flex:0 0 44px;text-align:center;font-weight:700;font-size:16px;padding:4px 0;
  border-radius:9px;background:rgba(255,255,255,.06)}
.bar{position:relative;flex:1;height:20px;border-radius:7px;background:var(--on);overflow:hidden}
.bar::after{content:"";position:absolute;inset:0;
  background-image:linear-gradient(90deg,rgba(11,16,32,.55) 1px,transparent 1px);
  background-size:calc(100% / 24) 100%}
.bar i{position:absolute;top:0;bottom:0;border-radius:5px;z-index:1;
  background:linear-gradient(180deg,var(--off1),var(--off2))}
.t{flex:0 0 168px;font-size:16px;font-weight:600;line-height:1.3;font-variant-numeric:tabular-nums}
.t.free{color:var(--on-text);font-weight:500;opacity:.85}

.axis{min-height:22px;padding:0}
.axis .q{background:none}
.ax{position:relative;flex:1;height:18px;color:var(--muted);font-size:13px;font-weight:600}
.ax span{position:absolute;top:0;transform:translateX(-50%)}
.axis .t{font-size:13px;font-weight:600;color:var(--muted)}

.foot{display:flex;align-items:center;gap:20px;color:var(--muted);font-size:13px;padding:2px 4px 0}
.foot div{display:flex;align-items:center;gap:7px}
.foot u{display:inline-block;width:14px;height:14px;border-radius:4px;text-decoration:none}
.foot .src{margin-left:auto}
@media (max-width:760px){#schedule{width:100%}}
"""

BOLT = (
    '<svg width="28" height="28" viewBox="0 0 24 24" fill="#0b1020">'
    '<path d="M13.2 2 4.5 13.6h6.1L9.8 22l8.7-11.6h-6.1z"/></svg>'
)


def _day_html(day: dict, today: date) -> str:
    d = parse_date(day["date"])
    title = weekday_name(day["date"]) or day["date"]
    sub = f"{d.day} {MONTHS[d.month - 1]}" if d else ""
    pill = ""
    if d == today:
        pill = '<span class="pill">Сьогодні</span>'
    elif d == today + timedelta(days=1):
        pill = '<span class="pill">Завтра</span>'

    total = len(day["slots"])
    with_outage = sum(1 for _, raw in day["slots"] if parse_intervals(raw))
    cnt = f"відключення: {with_outage} з {total} черг" if with_outage else "відключень немає"

    axis = "".join(
        f'<span style="left:{h / 24 * 100:.4f}%">{h}</span>' for h in range(0, 24, 3)
    )
    rows = [
        f'<div class="row axis"><div class="q"></div><div class="ax">{axis}</div>'
        '<div class="t">Час відключень</div></div>'
    ]
    for i, (queue, raw) in enumerate(day["slots"]):
        iv = parse_intervals(raw)
        segs = "".join(
            f'<i style="left:{a / 1440 * 100:.4f}%;width:{(b - a) / 1440 * 100:.4f}%"></i>'
            for a, b in iv
        )
        labels = interval_labels(raw, iv)
        if labels:
            cell = '<div class="t">' + "<br>".join(html.escape(x) for x in labels) + "</div>"
        else:
            cell = '<div class="t free">✓ світло є</div>'
        grp = " g" if i and i % 2 == 0 else ""  # x.1 і x.2 — одна група
        rows.append(
            f'<div class="row{grp}"><div class="q">{html.escape(queue)}</div>'
            f'<div class="bar">{segs}</div>{cell}</div>'
        )

    return (
        f'<section class="day"><div class="dh"><h2>{html.escape(title)}</h2>'
        f'<span class="dt">{html.escape(sub)}</span>{pill}<span class="cnt">{cnt}</span></div>'
        + "".join(rows)
        + "</section>"
    )


def render_html(days: list[dict], stamp: str, today: date | None = None) -> str:
    today = today or datetime.now().date()
    body = "".join(_day_html(d, today) for d in days[:MAX_DAYS])
    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Графік відключень</title>
<style>{CSS}</style></head>
<body>
<div id="schedule">
  <div class="top">
    <div class="logo">{BOLT}</div>
    <div><h1>Графік відключень</h1><p>Рівненська область · за чергами</p></div>
    <div class="stamp"><small>Оновлено</small><strong>{html.escape(stamp)}</strong></div>
  </div>
  {body}
  <div class="foot">
    <div><u style="background:linear-gradient(180deg,var(--off1),var(--off2))"></u>немає світла</div>
    <div><u style="background:var(--on)"></u>світло є</div>
    <span class="src">⚡ Дорожній Прогноз</span>
  </div>
</div>
</body></html>"""


if __name__ == "__main__":  # python render.py → preview.html
    from pathlib import Path
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Kyiv")).date()
    out = Path(__file__).with_name("preview.html")
    out.write_text(render_html(demo_days(1, today), "15:32", today), encoding="utf-8")
    print(f"Готово: {out}")
