#!/usr/bin/env python3
"""
Powiadomienia o wolnych miejscach w grupach (UniTime AGH, eksport CSV listy zajęć).

Źródło: na plan.agh.edu.pl widok "Lista" z włączoną kolumną "Zapisy"
(i jeśli się da, także "Limit") -> Eksport -> CSV. Link do tego eksportu
podajesz w PLAN_CSV_URL albo --url.

Co wykrywa:
  * jeśli CSV ma kolumnę Limit:   🟢 grupa miała komplet, a teraz ma wolne miejsce
                                  🔴 wolne miejsce znów zajęte
  * jeśli nie ma kolumny Limit:   🟡 spadła liczba zapisanych (ktoś się wypisał)
    (limit możesz podać ręcznie: --limit "Inżynieria Jakości|CWL 1b=11")

Użycie:
    python seats.py --once
    python seats.py --interval 5 --only "Inżynieria Jakości,Machine learning"
    python seats.py --once --csv meetings.csv      # test na pobranym pliku

Powiadomienia (Telegram/Discord) konfiguruje się tak samo jak w watcher.py.
"""

import argparse
import csv
import io
import os
import random
import re
import sys
import time
import urllib.error
from datetime import datetime

MIN_SECONDS = 10

from watcher import MIN_INTERVAL_MIN, fetch, fix_console, load_state, notify, save_state

LIMIT_COLUMNS = ("Limit", "Limit miejsc", "Maks. zapisy", "Max")
ENROLL_COLUMNS = ("Zapisy", "Zapisanych", "Enrollment")


def first_int(value):
    m = re.search(r"\d+", value or "")
    return int(m.group()) if m else None


def pick(row, names):
    for n in names:
        if n in row and (row[n] or "").strip():
            return row[n]
    return None


def parse_csv(text):
    """Zwraca (groups, has_limit). groups: klucz -> {title, kind, group, enrolled, limit}."""
    text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if not any(c in header for c in ENROLL_COLUMNS):
        raise RuntimeError("W CSV nie ma kolumny 'Zapisy' – włącz ją w widoku listy przed eksportem.")
    has_limit = any(c in header for c in LIMIT_COLUMNS)

    groups = {}
    for row in reader:
        title = (row.get("Tytuł") or row.get("Nazwa") or "?").strip()
        kind = (row.get("Typ") or "").strip()
        grp = (row.get("Grupa") or "").strip()
        key = f"{title} | {kind} {grp}".strip()
        enrolled = first_int(pick(row, ENROLL_COLUMNS))
        limit = first_int(pick(row, LIMIT_COLUMNS)) if has_limit else None
        if enrolled is None:
            continue
        g = groups.setdefault(key, {"title": title, "kind": kind, "group": grp,
                                    "enrolled": enrolled, "limit": limit})
        # wszystkie terminy grupy mają te same liczby; na wszelki wypadek bierzemy max
        g["enrolled"] = max(g["enrolled"], enrolled)
        if limit is not None:
            g["limit"] = max(g["limit"] or 0, limit)
    return groups, has_limit


def apply_manual_limits(groups, manual):
    """manual: lista 'fragment klucza=limit', np. 'Jakości|CWL 1b=11'."""
    for item in manual or []:
        if "=" not in item:
            continue
        pattern, value = item.rsplit("=", 1)
        parts = [p.strip().lower() for p in pattern.split("|") if p.strip()]
        for key, g in groups.items():
            if all(p in key.lower() for p in parts):
                g["limit"] = int(value)


G = "\x00"  # znacznik miejsca na numer grupy (podmieniany przy scalaniu)


def describe(g):
    lim = f"/{g['limit']}" if g.get("limit") else ""
    return f"{g['kind']} {G} ({g['enrolled']}{lim})"


def diff(old, new):
    """Zwraca listę (priorytet, tytuł, grupa, opis) zdarzeń; w opisie G = miejsce na grupę."""
    events = []
    for key, g in new.items():
        prev = old.get(key)
        if prev is None:
            continue
        limit = g.get("limit")
        if limit:
            was_free = prev["enrolled"] < (prev.get("limit") or limit)
            free = limit - g["enrolled"]
            if free > 0 and not was_free:
                events.append((0, g["title"], g["group"], f"🟢 WOLNE MIEJSCE: {describe(g)} – wolnych: {free}"))
            elif free <= 0 and was_free:
                events.append((2, g["title"], g["group"], f"🔴 znów komplet: {describe(g)}"))
            elif free > 0 and g["enrolled"] < prev["enrolled"]:
                events.append((1, g["title"], g["group"], f"🟢 kolejne wolne miejsce: {describe(g)} – wolnych: {free}"))
        elif g["enrolled"] < prev["enrolled"]:
            events.append((1, g["title"], g["group"],
                           f"🟡 ktoś się wypisał: {g['kind']} {G} – {prev['enrolled']} → {g['enrolled']}"))
    return events


def merge_lines(events):
    """Łączy identyczne komunikaty dla podgrup (np. CWL 1, 1a, 1b) w jedną linię."""
    merged = {}
    for prio, title, grp, line in sorted(events):
        merged.setdefault((title, prio, line), []).append(grp)
    out = {}
    for (title, prio, line), grps in merged.items():
        out.setdefault(title, []).append(line.replace(G, ", ".join(grps)))
    return out


QUIET = False  # w trybie szybkim nie zaśmiecamy konsoli linijkami "bez zmian"


def check(url, state_path, csv_file, only, manual_limits):
    text = open(csv_file, encoding="utf-8-sig").read() if csv_file else fetch(url)
    if "BEGIN:VCALENDAR" in text[:200]:
        raise RuntimeError("To jest link do iCalendar, a potrzebny jest link do eksportu CSV listy.")

    groups, has_limit = parse_csv(text)
    apply_manual_limits(groups, manual_limits)
    if only:
        needles = [n.strip().lower() for n in only.split(",") if n.strip()]
        groups = {k: v for k, v in groups.items() if any(n in k.lower() for n in needles)}

    saved = load_state(state_path)
    save_state(state_path, {"groups": groups})
    if saved is None:
        known = sum(1 for g in groups.values() if g.get("limit"))
        full = sum(1 for g in groups.values() if g.get("limit") and g["enrolled"] >= g["limit"])
        msg = f"✅ Śledzę zapisy w {len(groups)} grupach."
        if known:
            msg += f"\nZ limitem: {known} (w tym pełnych: {full})."
        if not has_limit:
            msg += ("\nℹ️ W CSV nie ma kolumny Limit – powiadomię, gdy spadnie liczba zapisanych."
                    "\nLimit możesz podać ręcznie opcją --limit.")
        if QUIET:
            print()
        notify(msg)
        return

    events = diff(saved.get("groups", {}), groups)
    if not events:
        line = f"[{datetime.now():%H:%M:%S}] bez zmian w zapisach ({len(groups)} grup)"
        if QUIET:
            print(line, end="\r", flush=True)
        else:
            print(line, flush=True)
        return

    if QUIET:
        print()  # nie nadpisuj linijki statusu
    lines = ["🎓 Zmiany w zapisach"]
    for title, msgs in merge_lines(events).items():
        lines.append("")
        lines.append(f"▶ {title}")
        lines.extend("  " + m for m in msgs)
    # @everyone tylko gdy jest wolne miejsce – "znów komplet" przychodzi po cichu
    notify("\n".join(lines), ping=any(prio <= 1 and "🟢" in line for prio, _, _, line in events))


def main():
    fix_console()
    p = argparse.ArgumentParser(description="Powiadomienia o wolnych miejscach w grupach (UniTime AGH).")
    p.add_argument("--url", default=os.environ.get("PLAN_CSV_URL") or None, help="link do eksportu CSV listy zajęć")
    p.add_argument("--state", default="seats_state.json")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=5, help="minuty między sprawdzeniami (min. 2)")
    p.add_argument("--seconds", type=float,
                   help=f"tryb szybki: sekundy między sprawdzeniami (min. {MIN_SECONDS})")
    p.add_argument("--hours", help="sprawdzaj tylko w tych godzinach, np. 7-23")
    p.add_argument("--only", default=os.environ.get("PLAN_FILTER"),
                   help="tylko pasujące przedmioty/grupy, np. 'Jakości,Machine learning'")
    p.add_argument("--limit", action="append", default=[l for l in os.environ.get("PLAN_LIMITS", "").split(";") if l],
                   help="ręczny limit, np. --limit 'Jakości|CWL 1b=11' (można powtarzać)")
    p.add_argument("--csv", help="zamiast pobierać, wczytaj lokalny plik CSV (do testów)")
    args = p.parse_args()

    if not args.url and not args.csv:
        p.error("podaj link do eksportu CSV: --url '...' albo zmienną PLAN_CSV_URL")

    def run():
        check(args.url, args.state, args.csv, args.only, args.limit)

    if args.once:
        try:
            run()
        except Exception as e:
            print(f"[!] Błąd: {e}", file=sys.stderr)
            sys.exit(1)
        return

    global QUIET
    if args.seconds:
        base = max(args.seconds, MIN_SECONDS)
        QUIET = True
        print(f"Tryb szybki: sprawdzam co ~{base:g} s. Zatrzymanie: Ctrl+C.", flush=True)
    else:
        base = max(args.interval, MIN_INTERVAL_MIN) * 60

    hours = None
    if args.hours:
        a, b = (int(x) for x in args.hours.split("-"))
        hours = (a, b)

    failures, warned = 0, False
    while True:
        if hours and not (hours[0] <= datetime.now().hour <= hours[1]):
            time.sleep(60)
            continue
        wait = base
        try:
            run()
            failures, warned = 0, False
        except urllib.error.HTTPError as e:
            failures += 1
            retry = e.headers.get("Retry-After") if e.headers else None
            # 429 / 503 = serwer prosi o zwolnienie – słuchamy go
            wait = float(retry) if retry and retry.isdigit() else min(base * 2 ** failures, 600)
            print(f"\n[!] HTTP {e.code} – zwalniam, następna próba za {wait:.0f} s", file=sys.stderr, flush=True)
        except Exception as e:
            failures += 1
            wait = min(base * 2 ** failures, 600)
            print(f"\n[!] Błąd ({failures}): {e} – następna próba za {wait:.0f} s", file=sys.stderr, flush=True)
        if failures >= 6 and not warned:
            notify("⚠️ Od dłuższego czasu nie mogę pobrać zapisów – sprawdź, czy link działa i czy serwer nie blokuje.")
            warned = True
        # losowy rozrzut ±20%, żeby zapytania nie szły w idealnie równym rytmie
        time.sleep(wait * random.uniform(0.8, 1.2))

if __name__ == "__main__":
    main()
