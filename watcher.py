#!/usr/bin/env python3
"""
Śledzenie zmian w planie zajęć AGH (UniTime, eksport iCalendar).

Pobiera kalendarz z linku eksportu, rozwija zajęcia cykliczne na pojedyncze
terminy, porównuje z poprzednim stanem i wysyła powiadomienie o zmianach
(Telegram, Discord albo tylko wypisanie w konsoli).

Tylko biblioteka standardowa Pythona 3.8+.

Użycie:
    python watcher.py --once              # jedno sprawdzenie (cron / GitHub Actions)
    python watcher.py --interval 10       # działa w pętli, sprawdza co 10 minut
    python watcher.py --once --ics plik.ics   # test na lokalnym pliku

Konfiguracja przez zmienne środowiskowe:
    PLAN_URL            link do eksportu (domyślnie ten z FiIS-PIS-2 S2)
    TELEGRAM_TOKEN      token bota Telegram (opcjonalnie)
    TELEGRAM_CHAT_ID    id czatu Telegram (opcjonalnie)
    DISCORD_WEBHOOK     URL webhooka Discord (opcjonalnie)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta

DEFAULT_URL = "https://plan.agh.edu.pl/UniTime/export?x=-3ov5g1vumac78p8z5qslfnd9dmufa3121"
USER_AGENT = "agh-plan-watch/1.0 (prywatne powiadomienia o zmianach w planie)"
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
DAY_NAMES = ["pn", "wt", "śr", "cz", "pt", "sb", "nd"]
MIN_INTERVAL_MIN = 2  # nie odpytujemy serwera uczelni częściej


# ---------------------------------------------------------------- pobieranie

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------- parsowanie ICS

def unfold(text):
    """Łączy zawinięte linie ICS (kontynuacja zaczyna się spacją/tabem)."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_ics(text):
    """Zwraca (nazwa_kalendarza, lista słowników VEVENT)."""
    events, current, calname = [], None, ""
    depth_other = 0  # pomijamy VTIMEZONE itp.
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if line.startswith("BEGIN:") and line != "BEGIN:VCALENDAR":
            depth_other += 1
            continue
        if line.startswith("END:") and line != "END:VCALENDAR":
            depth_other = max(0, depth_other - 1)
            continue
        if ":" not in line:
            continue
        name_part, value = line.split(":", 1)
        name = name_part.split(";", 1)[0].upper()
        if current is None:
            if name == "X-WR-CALNAME" and depth_other == 0:
                calname = unescape(value)
            continue
        if name == "EXDATE":
            current.setdefault("EXDATE", []).extend(value.split(","))
        else:
            current[name] = unescape(value)
    return calname, events


def parse_dt(value):
    """'20261005T164500' -> datetime (czas lokalny, bez strefy)."""
    value = value.strip().rstrip("Z")
    if "T" in value:
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    return datetime.strptime(value, "%Y%m%d")


def expand(event):
    """Rozwija wydarzenie na listę dat wystąpień (obsługuje RRULE WEEKLY z UniTime)."""
    start = parse_dt(event["DTSTART"])
    rrule = event.get("RRULE")
    if not rrule:
        return [start.date()]

    rule = dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)
    if rule.get("FREQ") != "WEEKLY":
        # UniTime używa tylko WEEKLY; na wszelki wypadek zwracamy sam start
        return [start.date()]

    interval = int(rule.get("INTERVAL", "1"))
    days = [WEEKDAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d] or [start.weekday()]
    # UNTIL jest w UTC; zajęcia odbywają się w dzień, więc data UTC = data lokalna
    until = parse_dt(rule["UNTIL"]).date() if "UNTIL" in rule else None
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    if until is None and count is None:
        until = start.date() + timedelta(days=366)

    excluded = {parse_dt(x).date() for x in event.get("EXDATE", []) if x.strip()}
    week_start = start.date() - timedelta(days=start.weekday())
    result, produced = [], 0
    while True:
        for wd in sorted(days):
            d = week_start + timedelta(days=wd)
            if d < start.date():
                continue
            if until and d > until:
                return result
            if count is not None and produced >= count:
                return result
            produced += 1
            if d not in excluded:
                result.append(d)
        week_start += timedelta(weeks=interval)
        if until is None and week_start > start.date() + timedelta(days=400):
            return result


def build_state(events):
    """
    Stan = słownik: klucz grupy zajęciowej -> lista terminów.
    Klucz grupy to "Przedmiot | Typ grupa" (np. "Kryptografia | CWL 1"),
    dzięki czemu wykrywamy zmiany nawet gdy UniTime nada wydarzeniu nowe UID.
    """
    state = {}
    for ev in events:
        if ev.get("STATUS", "").upper() == "CANCELLED":
            continue
        summary = ev.get("SUMMARY", "?").strip()
        desc_lines = ev.get("DESCRIPTION", "").split("\n")
        group = desc_lines[0].strip() if desc_lines else ""
        teacher = desc_lines[1].strip() if len(desc_lines) > 1 else ""
        start, end = parse_dt(ev["DTSTART"]), parse_dt(ev.get("DTEND", ev["DTSTART"]))
        key = f"{summary} | {group}"
        for d in expand(ev):
            state.setdefault(key, []).append({
                "date": d.isoformat(),
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "room": ev.get("LOCATION", "").strip(),
                "teacher": teacher,
            })
    for key in state:
        state[key].sort(key=lambda o: (o["date"], o["start"]))
    return state


# ---------------------------------------------------------------- porównanie

def fmt_occ(o):
    d = date.fromisoformat(o["date"])
    return f"{DAY_NAMES[d.weekday()]} {d.day}.{d.month:02d} {o['start']}–{o['end']}, {o['room']}"


def change_desc(old, new):
    parts = []
    if (old["start"], old["end"]) != (new["start"], new["end"]):
        parts.append(f"godz. {old['start']}–{old['end']} → {new['start']}–{new['end']}")
    if old["room"] != new["room"]:
        parts.append(f"sala {old['room']} → {new['room']}")
    if old["teacher"] != new["teacher"]:
        parts.append(f"prowadzący {old['teacher']} → {new['teacher']}")
    return "; ".join(parts)


def short_date(iso):
    d = date.fromisoformat(iso)
    return f"{DAY_NAMES[d.weekday()]} {d.day}.{d.month:02d}"


def compress_changes(changes):
    """[(data, opis)] -> linie; ta sama zmiana w wielu terminach = jedna linia."""
    by_desc = {}
    for d, desc in changes:
        by_desc.setdefault(desc, []).append(d)
    lines = []
    for desc, dates in by_desc.items():
        dates.sort()
        if len(dates) >= 3:
            lines.append(f"✏️ {len(dates)} terminów ({short_date(dates[0])} – {short_date(dates[-1])}): {desc}")
        else:
            lines.extend(f"✏️ {short_date(d)}: {desc}" for d in dates)
    return lines


def diff_states(old, new):
    """Zwraca listę sekcji (nagłówek, [linie]) opisujących zmiany."""
    sections = []
    for key in sorted(set(old) | set(new)):
        before = {(o["date"], o["start"]): o for o in old.get(key, [])}
        after = {(o["date"], o["start"]): o for o in new.get(key, [])}
        if before == after:
            continue

        lines, changes = [], []
        if key not in old:
            lines.append(f"🆕 nowa grupa, {len(after)} termin(y/ów), pierwszy: {fmt_occ(min(after.values(), key=lambda o: (o['date'], o['start'])))}")
            sections.append((key, lines))
            continue
        if key not in new:
            lines.append("❌ grupa zniknęła z planu")
            sections.append((key, lines))
            continue

        removed = {k: v for k, v in before.items() if k not in after}
        added = {k: v for k, v in after.items() if k not in before}

        # Ta sama data i godzina, ale inna sala/prowadzący
        for k in sorted(set(before) & set(after)):
            if before[k] != after[k]:
                changes.append((k[0], change_desc(before[k], after[k])))

        # Ta sama data, inna godzina -> przesunięcie w obrębie dnia
        removed_by_date = {}
        for k, v in removed.items():
            removed_by_date.setdefault(v["date"], []).append(k)
        for k in sorted(added):
            same_day = removed_by_date.get(added[k]["date"])
            if same_day:
                old_k = same_day.pop(0)
                changes.append((added[k]["date"], change_desc(removed.pop(old_k), added[k])))
                added[k] = None
        lines.extend(compress_changes(changes))
        for k in sorted(removed):
            lines.append("➖ odwołany: " + fmt_occ(removed[k]))
        for k in sorted(k for k, v in added.items() if v):
            lines.append("➕ nowy termin: " + fmt_occ(added[k]))

        if lines:
            sections.append((key, lines))
    return sections


def render(calname, sections, max_lines_per_group=8):
    out = [f"📅 Zmiany w planie: {calname}" if calname else "📅 Zmiany w planie"]
    for key, lines in sections:
        out.append("")
        out.append(f"▶ {key}")
        shown = lines[:max_lines_per_group]
        out.extend("  " + l for l in shown)
        if len(lines) > len(shown):
            out.append(f"  … i jeszcze {len(lines) - len(shown)} zmian(y)")
    return "\n".join(out)


# ---------------------------------------------------------------- powiadomienia

def split_message(text, limit):
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current += line[:limit] + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    urllib.request.urlopen(req, timeout=30).read()


def notify(text, ping=False):
    """ping=True -> na Discordzie dodaje @everyone (mocne powiadomienie na telefonie).
    Można wyłączyć zmienną DISCORD_PING=0."""
    print(text, flush=True)
    ping = ping and os.environ.get("DISCORD_PING", "1") != "0"
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if token and chat:
        for chunk in split_message(text, 4000):
            try:
                post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                          {"chat_id": chat, "text": chunk, "disable_web_page_preview": True})
            except Exception as e:
                print(f"[!] Telegram: {e}", file=sys.stderr)
    if webhook:
        for i, chunk in enumerate(split_message(text, 1900)):
            payload = {"content": chunk, "allowed_mentions": {"parse": []}}
            if ping and i == 0:
                payload = {"content": "@everyone\n" + chunk, "allowed_mentions": {"parse": ["everyone"]}}
            try:
                post_json(webhook, payload)
            except Exception as e:
                print(f"[!] Discord: {e}", file=sys.stderr)


# ---------------------------------------------------------------- główna logika

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def check(url, state_path, ics_file=None, filter_text=None):
    """Jedno sprawdzenie. Zwraca True, jeśli wykryto zmiany."""
    text = open(ics_file, encoding="utf-8").read() if ics_file else fetch(url)
    if "BEGIN:VCALENDAR" not in text:
        raise RuntimeError("Odpowiedź nie jest kalendarzem – czy link eksportu nadal działa?")

    calname, events = parse_ics(text)
    new = build_state(events)
    if filter_text:
        needles = [f.strip().lower() for f in filter_text.split(",") if f.strip()]
        new = {k: v for k, v in new.items() if any(n in k.lower() for n in needles)}

    filter_key = ",".join(sorted(n.strip().lower() for n in (filter_text or "").split(",") if n.strip()))
    saved = load_state(state_path)
    if saved is not None and saved.get("filter", "") != filter_key:
        # zmiana listy śledzonych przedmiotów to nie zmiana planu – zaczynamy od nowa
        save_state(state_path, {"calname": calname, "groups": new, "filter": filter_key})
        notify(f"🔧 Zmieniono listę śledzonych przedmiotów – teraz {len(new)} grup zajęciowych.")
        return False
    if saved is None:
        save_state(state_path, {"calname": calname, "groups": new, "filter": filter_key})
        total = sum(len(v) for v in new.values())
        notify(f"✅ Zaczynam śledzić: {calname or url}\n{len(new)} grup zajęciowych, {total} terminów.")
        return False

    sections = diff_states(saved.get("groups", {}), new)
    if sections:
        notify(render(calname, sections))
        save_state(state_path, {"calname": calname, "groups": new, "filter": filter_key})
        return True

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] bez zmian ({len(new)} grup)", flush=True)
    return False


def fix_console():
    """Konsola Windows bywa w cp1250/cp1252 – emoji by ją wysypały."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    fix_console()
    p = argparse.ArgumentParser(description="Powiadomienia o zmianach w planie AGH (UniTime).")
    p.add_argument("--url", default=os.environ.get("PLAN_URL") or DEFAULT_URL)
    p.add_argument("--state", default="state.json", help="plik ze stanem (domyślnie state.json)")
    p.add_argument("--once", action="store_true", help="jedno sprawdzenie i koniec")
    p.add_argument("--interval", type=float, default=10, help="minuty między sprawdzeniami (min. 5)")
    p.add_argument("--only", default=os.environ.get("PLAN_FILTER"),
                   help="śledź tylko pasujące przedmioty/grupy, np. 'Kryptografia,Smart dom'")
    p.add_argument("--ics", help="zamiast pobierać, wczytaj lokalny plik .ics (do testów)")
    args = p.parse_args()

    if args.once:
        try:
            check(args.url, args.state, args.ics, args.only)
        except Exception as e:
            print(f"[!] Błąd: {e}", file=sys.stderr)
            sys.exit(1)
        return

    interval = max(args.interval, MIN_INTERVAL_MIN) * 60
    failures = 0
    while True:
        try:
            check(args.url, args.state, args.ics, args.only)
            failures = 0
        except Exception as e:
            failures += 1
            print(f"[!] Błąd ({failures}): {e}", file=sys.stderr)
            if failures == 6:  # ok. godzina bez danych – dajmy znać raz
                notify(f"⚠️ Od dłuższego czasu nie mogę pobrać planu: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
