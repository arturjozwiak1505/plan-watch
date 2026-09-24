# agh-plan-watch

Powiadomienia o zmianach w planie zajęć AGH (UniTime). Skrypt pobiera eksport
iCalendar planu, rozwija zajęcia cykliczne na pojedyncze terminy i porównuje je
z poprzednim stanem. Wykrywa:

- ➕ nowe terminy i 🆕 nowe grupy,
- ➖ odwołane terminy i ❌ usunięte grupy,
- ✏️ zmiany godzin, sal i prowadzących.

Wymaga tylko Pythona 3.8+ (bez dodatkowych bibliotek).

## Wolne miejsca w grupach (`seats.py`)

Liczba zapisanych jest tylko w eksporcie **CSV widoku listy** (nie w iCalendar):

1. Na plan.agh.edu.pl wybierz plan i przełącz widok na **Lista**.
2. W menu kolumn włącz **Zapisy** (i **Limit**, jeśli jest taka opcja).
3. Eksport → CSV. Link do pobranego pliku znajdziesz w historii pobrań
   przeglądarki (Ctrl+J → prawy przycisk → „Kopiuj adres linku”).
   Powinien wyglądać jak `https://plan.agh.edu.pl/UniTime/export?x=...`.

Uruchomienie:

    python seats.py --url "LINK_DO_CSV" --interval 5 --only "Jakości,Machine learning"

- Z kolumną **Limit**: 🟢 gdy w pełnej grupie zwolni się miejsce, 🔴 gdy znów jest komplet.
- Bez niej: 🟡 gdy spadnie liczba zapisanych. Limit możesz podać ręcznie, wtedy
  działa jak wyżej: `--limit "Jakości|CWL=11"` (fragmenty nazwy oddzielone `|`,
  opcję można powtarzać; w GitHub Actions: zmienna `PLAN_LIMITS`, wpisy oddzielone `;`).

W GitHub Actions dodaj sekret `PLAN_CSV_URL` – krok sprawdzania miejsc włączy się sam.

## Szybki start (na własnym komputerze)

    python watcher.py --interval 10

Pierwsze uruchomienie zapisuje stan do `state.json`, kolejne zgłaszają zmiany.
Żeby śledzić tylko wybrane przedmioty:

    python watcher.py --interval 10 --only "Kryptografia,Machine learning"

Inny plan? Na plan.agh.edu.pl wybierz plan → Eksport → skopiuj link i podaj go
przez `--url "..."` albo zmienną `PLAN_URL`.

## Powiadomienia na telefon (Telegram)

1. Napisz do @BotFather na Telegramie, wyślij `/newbot`, zapisz token.
2. Napisz cokolwiek do swojego nowego bota.
3. Otwórz `https://api.telegram.org/bot<TOKEN>/getUpdates` i odczytaj `chat.id`.
4. Ustaw zmienne:

       # Linux/macOS
       export TELEGRAM_TOKEN="123:ABC..."
       export TELEGRAM_CHAT_ID="123456789"
       # Windows PowerShell
       $env:TELEGRAM_TOKEN="123:ABC..."
       $env:TELEGRAM_CHAT_ID="123456789"

Zamiast Telegrama można użyć Discorda: `DISCORD_WEBHOOK` = URL webhooka kanału.

## Działanie 24/7 za darmo (GitHub Actions)

1. Utwórz repozytorium na GitHubie i wrzuć do niego te pliki.
2. Settings → Secrets and variables → Actions → **Secrets**: dodaj
   `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (opcjonalnie `PLAN_URL`, `DISCORD_WEBHOOK`).
3. Opcjonalnie w zakładce **Variables**: `PLAN_FILTER` = np. `Kryptografia,Smart dom`.
4. Actions → „Sprawdź plan AGH” → **Run workflow** (pierwsze uruchomienie zapisze stan).

Uwagi:
- W repozytorium **publicznym** minuty Actions są bez limitu. W prywatnym darmowy
  limit to 2000 min/mies., a sprawdzanie co 15 min zużywa ok. 2900 – zmień wtedy
  cron na `*/30 * * * *`.
- GitHub wyłącza zaplanowane workflowy w publicznych repo po 60 dniach bez commitów.
  Każda wykryta zmiana robi commit, ale jeśli plan długo stoi, dostaniesz maila
  i wystarczy kliknąć „Enable workflow”.

## Dobre obyczaje

Nie ustawiaj sprawdzania częściej niż co 5 minut (skrypt i tak na to nie pozwala)
– to serwer uczelni, a plan nie zmienia się co minutę.
