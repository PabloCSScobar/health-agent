# Plan implementacji - kolejne funkcje (2026-09-19)

Kolejność wynika z zależności i z tego, co już nas ugryzło. Każdy punkt ma:
cel, decyzje (rozstrzygnięte na podstawie sprawdzonych faktów), kroki,
pliki, weryfikację. Szacunki kosztu = tokeny/API, nie "dni pracy". Stan
tła: `TODO.md` (co i dlaczego), `scripts/README.md` (historia decyzji).

Fakty sprawdzone przed planem (nie założenia):
- WSL ma systemd, ale wdrożenie aplikacji zostało ujednolicone przez Docker
  Compose dla WSL i VPS. Docker Desktop (Windows) podnosi dystrybucję i
  kontenery przy logowaniu; na VPS autostart zapewnia systemowa usługa
  Dockera oraz `restart: unless-stopped`.
- python-telegram-bot 22.8: jest `MessageReactionHandler`; reakcje
  przychodzą tylko gdy `run_polling(allowed_updates=...)` zawiera
  `message_reaction`.
- Biblioteka Fitatu: `login(email, password)`, `refresh_access_token`,
  `get_day_plan(date)` - refresh flow jest, brakuje tylko poświadczeń.
- `Dockerfile` i pełny Compose są wdrożone; migracja na VPS pozostaje operacją utrzymaniową.

---

## 1. Przenośne wdrożenie i autostart (WDROŻONE W KODZIE 2026-09-18)

**Cel:** jedna instalacja dla WSL i VPS; baza, webhook, scheduler i bot
wstają razem, a padnięte procesy są restartowane.

**Decyzja:** Docker Compose, nie osobne unity systemd aplikacji. Systemd na
VPS zarządza Dockerem; na Windows autostart demona zapewnia Docker Desktop.
Tailscale działa na hoście i obsługuje HTTPS przed lokalnym portem API.

**Zrealizowane:**
1. `Dockerfile`: Python 3.12 + `uv.lock`, klient PostgreSQL, proces non-root.
2. Compose: `db`, jednorazowe `migrate`, jedno-workerowe `api` ze schedulerem
   i `bot`; healthchecki, zależności i `restart: unless-stopped`.
3. `deploy/install.sh`, `doctor.sh`, `update.sh` oraz instrukcja WSL/VPS.
4. Backup używa `pg_dump` względem `DATABASE_URL`, bez socketa Dockera.
5. `.env.example` jest pełnym szablonem wdrożeniowym; instalator generuje
   nowe hasło i sekret tylko przy nowej instalacji, nie zmienia istniejącej bazy.
6. Środowiska są rozdzielone przez `APP_ENV` i `SCHEDULER_ENABLED`;
   development ma osobnego bota Telegram i domyślnie nie uruchamia schedulera.

**Weryfikacja automatyczna:** build i uruchomienie obrazu non-root z klientem
PostgreSQL 16, parser Compose, składnia skryptów, kompilacja Pythona, CLI,
test jednostkowy backupu oraz izolowany test PostgreSQL 16: wszystkie migracje,
syntetyczny rekord, prawdziwy `pg_dump`, restore i porównanie danych.

**Weryfikacja operacyjna:** lokalne wdrożenie Compose przeszło diagnostykę,
a wdrożenie VPS pełny restart hosta, automatyczny powrót kontenerów, HTTPS,
webhook z telefonu i odpowiedź API. Lokalny scheduler i bot production są
wyłączone. Przy równoległym development używać osobnego tokenu Telegrama.

**Ryzyko:** WSL nie startuje bez Docker Desktop lub innego procesu. Jeżeli
opcja „start at login” nie podnosi dystrybucji, użyć Harmonogramu zadań Windows
z `wsl.exe -d Ubuntu-24.04 -- true`.

---

## 2. Pętla zwrotna 👍/👎 z Telegrama (WDROŻONE W KODZIE 2026-09-19)

**Cel:** każda odpowiedź asystenta ma przypięty `agent_run` (korzeń
drzewa); reakcja 👍/👎 pod wiadomością zapisuje ocenę; `health-agent
feedback` pokazuje 👎 z pytaniem, odpowiedzią, agentem i narzędziami - to
jest materiał do poprawiania promptów zamiast zgadywania.

**Decyzje:** reakcje (nie przyciski inline) - zero tarcia, natywne w
Telegramie. Komentarz tekstowy opcjonalnie: odpowiedź (reply) na wiadomość
bota zaczynająca się od "👎"/"👍" albo "feedback:" zapisuje tekst do tej
samej oceny.

**Kroki:**
1. Migracja: `conversations.telegram_message_id` (bigint, index) + lista wszystkich części,
   `conversations.root_run_id` (FK agent_runs); tabela `feedback` (id,
   conversation_id, agent_run_id, rating -1/+1, comment, created_at).
2. `run_agent` zwraca dziś tylko output - dodać sposób odczytu id korzenia:
   contextvar `_last_root_run_id` ustawiany w `run_agent`, gdy
   `parent_run_id is None`; `ask_orchestrator_async` zwraca `(answer,
   root_run_id)` (albo nowa funkcja, żeby nie łamać CLI/eval).
3. `telegram.py`: `_reply` zwraca `Message` (pierwszy chunk) -> zapis
   `telegram_message_id(s)` + `root_run_id` w wierszu asystenta.
   `MessageReactionHandler(handle_reaction)`: z `update.message_reaction`
   bierze `message_id`, `new_reaction` (ReactionTypeEmoji: 👍/👎), szuka
   wiersza po `telegram_message_id`, upsert `feedback`. Reply-to z tekstem
   -> `comment`. `run_polling(allowed_updates=Update.ALL_TYPES)`.
4. CLI `health-agent feedback [--days 30] [--bad]`: lista ocen z
   pytaniem/odpowiedzią/agentem/narzędziami/kosztem; `--export plik.md` do
   przeglądu.
5. Dalsze wykorzystanie (jeszcze otwarte): `check_stale_sources`-style job NIE -
   zamiast tego `eval_agents.py feedback`: bierze ostatnie 👎, uruchamia te
   same pytania ponownie po zmianie promptu i pokazuje sędziego przed/po
   (regresja na realnych, nie syntetycznych pytaniach).

**Weryfikacja:** reakcja 👍 na Telegramie -> wiersz w `feedback` z właściwym
`agent_run_id` (sprawdzić, że to korzeń: orchestrator, nie specjalista);
reakcja zmieniona 👍->👎 -> upsert, nie drugi wiersz; reply z tekstem ->
komentarz; wiadomości sprzed migracji (bez message_id) -> reakcja
ignorowana z logiem, nie wyjątek.

**Koszt:** zero tokenów w runtime.

---

## 3. Running na Haiku 4.5

**Cel:** ~40% taniej na najdroższym agencie, jeśli jakość się utrzyma.

**Kroki:** `config/agents.yaml: running: anthropic:claude-haiku-4-5`; 3x
`eval_agents.py running` (21 przypadków + sędzia); porównać z 3x Sonnet
(dziś: 7/7, sędzia 5/5). Kryteria przejścia: 0 błędów strukturalnych
(narzędzia, konsultacja recovery), sędzia >= 4/5 w >= 19/21, i ręczny
przegląd 3 odpowiedzi "głębokich" (dryf, plan) - sędzia nie ocenia
trafności liczb. Jeśli pada tylko "plan na tydzień" -> zostawić Sonnet dla
running, ale to i tak ~1 pytanie/tydz.

**Koszt:** ~$0.6 (6 przebiegów).

---

## 4. `/sync` Intervals.icu (WDROŻONE W KODZIE 2026-09-19) i Fitatu (OTWARTE)

**Cel części ukończonej:** ręczne odświeżenie treningów i wellness korzysta
z dokładnie tego samego mechanizmu co scheduler, także gdy bot i API są
osobnymi procesami.

**Zrealizowane:**
1. `ingest/sync.py` wyznacza wspólne okno catch-up: 3 dni, zakładka od
   ostatniego udanego markera i limit 60 dni z ostrzeżeniem o obcięciu.
2. Transakcyjna blokada advisory PostgreSQL nie pozwala schedulerowi i
   Telegramowi wykonywać tego samego importu równolegle.
3. Pobranie, zapis `raw_payloads`, upserty i przesunięcie markera są w jednej
   transakcji. Błąd pozostawia poprzedni marker i wycofuje audyt oraz dane.
4. Telegram `/sync` uruchamia kod synchroniczny przez `asyncio.to_thread`;
   scheduler obsługuje zajętą blokadę jako bezpieczne pominięcie.
5. Jawny zakres CLI używa tej samej blokady i transakcji, lecz nie przesuwa
   markera automatycznego pollingu.

**Weryfikacja:** testy jednostkowe okna i rejestracji narzędzi oraz izolowany
PostgreSQL 16: wzajemne wykluczanie procesów, rollback po błędzie, atomowy
marker i brak przesunięcia markera przez ręczny zakres.

**Część otwarta - dzisiejsze jedzenie z Fitatu:** bezpośredni ingest pozostaje
zablokowany przez brak działającego refresh flow. Przed implementacją trzeba
rozstrzygnąć poświadczenia i ryzyko nieoficjalnego API. Health Connect pozostaje
źródłem kanonicznym. Jeśli funkcja powstanie, wpisy `source="fitatu_api"` mają
być tymczasowe: pełne zastąpienie dnia z Fitatu, usunięcie po nadejściu Health
Connect i agregowanie tylko jednego źródła, aby nie liczyć dnia podwójnie.

**Plan Fitatu po odblokowaniu:** przenieść klienta ze `scripts/test_fitatu.py`
do `ingest/fitatu.py`, dodać bezpieczny cache/refresh tokenu, normalizację
dnia, `refresh_today_nutrition()` i CLI. Nie dodawać Fitatu do schedulera;
wywoływać tylko na żądanie.

---

## 5. Analiza korelacji -> `knowledge` (lekcje)

**Cel:** wzorce "u Ciebie" ("po <6h snu EF spada", "HRV spada po 2 mocnych
dniach") liczone z danych, nie zgadywane, trafiają do digestu specjalistów
jako `lekcja` z liczbą obserwacji.

**Decyzja:** statystyka deterministycznie w Pythonie (Spearman na
rangach, n>=10, |rho|>=0.4, p<0.05 z aproksymacją t; bez scipy), LLM
(Haiku) tylko formułuje zdanie i rozstrzyga, czy to nowa lekcja czy
aktualizacja istniejącej. Zero "korelacji" z 5 punktów.

**Kroki:**
1. `tools/correlations.py`: `daily_frame(weeks=12)` - jeden wiersz na
   dzień: sleep_h, sleep_score, hrv, rhr, steps, run_km, run_load,
   run_ef (śr. z biegów dnia), run_start_hour (najpóźniejszy trening),
   kcal, protein_g, balance_kcal, weight (średnia 7d). Źródła: istniejące
   narzędzia recovery/running/nutrition/body - nie nowe zapytania.
2. Lista par z opóźnieniem (jawna, nie "wszystko ze wszystkim" - inaczej
   fałszywe odkrycia): sleep_h[t-1]->hrv[t]; sleep_h[t-1]->run_ef[t];
   run_load[t-1]+[t-2]->hrv[t]; run_start_hour[t]->sleep_score[t+1];
   balance_kcal (śr. 7d)->weight slope; protein_g[t-1]->run_ef[t];
   steps[t]->sleep_score[t+1]. `compute_correlations()` -> lista
   {pair, n, rho, p, direction, example_days}.
3. Job niedzielny (scheduler, konfigurowalny `CORRELATIONS_ENABLED`):
   istotne pary -> Haiku formułuje 1 zdanie po polsku z n i kierunkiem ->
   `add_knowledge(domain wg pary, kind="lekcja", source_type="agent",
   source_agent="correlations", confidence wg p)`; poprzednia lekcja tej
   samej pary -> `supersede`. Nieistotne po wcześniejszej istotnej ->
   dezaktywuj (wzorzec zniknął) z logiem.
4. Narzędzie `get_correlations()` dla recovery/running/nutrition (pełne
   liczby, nie tylko zdanie z digestu). CLI `health-agent correlations`
   (bez zapisu, do podglądu).
5. Eval: syntetyczny frame z wbudowaną korelacją -> wykryta; losowy ->
   nic; n<10 -> nic; lekcja zapisana raz, drugi przebieg = update nie
   duplikat.

**Weryfikacja na prawdziwych danych:** dziś ~35 dni wellness + 15 biegów +
2 dni jedzenia - większość par jeszcze nie osiągnie n>=10 z jedzeniem;
sleep->hrv i load->hrv już tak. Wynik pierwszego przebiegu obejrzeć ręcznie
przed włączeniem joba.

**Koszt:** ~$0.01/tydz. (Haiku, kilka zdań).

---

## 6. Dzienne i tygodniowe podsumowania (WDROŻONE W KODZIE 2026-09-19)

**Cel:** dzienny raport bieżącego dnia oraz tygodniowa wiadomość: km/obciążenie i forma, regeneracja, dieta i
bilans, waga - co poszło dobrze, co poprawić, jedna rzecz na następny
tydzień. Raport dzienny wrócił decyzją użytkownika 2026-09-19 i jest opcjonalny.

**Decyzja:** 4 specjalistów równolegle (`asyncio.gather`) z tym samym
pytaniem-szablonem ("Podsumuj tydzień pon-nd: 3-4 liczby, 1 zdanie oceny
względem poprzedniego tygodnia, 1 rekomendacja") - bez orchestratora
(delegowałby do jednego). Modele wg configu (running Sonnet/Haiku po pkt
3). Sklejone deterministycznie z nagłówkami i dzielone zgodnie z limitem Telegrama. Zapis jako `Conversation(agent="daily"|"weekly")`, żeby kolejne pytanie miało kontekst.

**Zrealizowane:** `agents/summaries.py` (`build_daily_summary`, `build_weekly_summary`), komendy Telegram `/daily` i `/weekly`, CLI `health-agent daily|weekly`, wspólny root run do feedbacku oraz joby cron
`day_of_week=sun, hour=20` w strefie Europe/Warsaw (scheduler jest UTC -
podać `timezone` w triggerze), settings `DAILY_SUMMARY_*`, `WEEKLY_SUMMARY_*` i `SUMMARY_TIMEZONE`; automatyczna wysyłka jest domyślnie wyłączona. Reguła w promptach
"tryb podsumowania: bez ❓, bez ofert pogłębienia".

**Weryfikacja:** CLI generuje 4 sekcje z liczbami z narzędzi (sprawdzić 2
liczby ręcznie w bazie); brak danych w domenie (np. siłownia) -> sekcja
"brak wpisów", nie zmyślona; sędzia (tryb analysis) >= 4/5 na każdej
sekcji; koszt jednego podsumowania zmierzony w `agent_runs` (oczekiwane
$0.10-0.15).

---

## Zależności i kolejność

Punkty 1, 2, część Intervals z 4 oraz 6 są gotowe. Następny samodzielny krok
to 5 (korelacje); punkt 3 (Haiku) jest niezależnym eksperymentem płatnym.
Część Fitatu z 4 pozostaje zablokowana przez poświadczenia i decyzję o
nieoficjalnym API. Po zmianach promptów/modeli: odpowiedni `eval_agents.py`
na osobnej bazie testowej, następnie commit.

---

## Odłożone - szkice, żeby nie planować od zera później

**Suplementy:** tabela `supplements` (nazwa, dawka, pora: `HH:MM` |
`meal:lunch`, aktywny), `supplement_log` (data, wzięty/pominięty);
przypomnienie o stałej porze = cron w schedulerze + `_send_telegram_message`
z pytaniem "wziąłeś? tak/nie" (odpowiedź parsuje orchestrator jak wpis);
"do obiadu" = heurystyka: nowe `nutrition_items` dziś w oknie 12-15 (po
pkt 4 - z API Fitatu jest typ posiłku, więc heurystyka staje się faktem).
NutritionCoach dostaje `get_supplements()`.

**Zdjęcia sylwetki (bez analizy):** `MessageHandler(filters.PHOTO)` ->
`data/photos/YYYY-MM-DD_<typ>.jpg` (gitignored), tabela `progress_photos`
(data, typ z podpisu: przód/bok/tył, waga i %tłuszczu z tego dnia z
`body_composition`, notatka); `/foto przód` -> album ostatnich N tego typu
z podpisami (waga, data). Backup: katalog dopisać do `backup_database`
(tar obok dumpu).

**Wiadomości głosowe:** `filters.VOICE` -> ogg -> transkrypcja lokalnie
(`faster-whisper`, model `small`, polski OK, CPU kilka sekund; zero danych
na zewnątrz poza tym, co i tak idzie do Anthropic jako tekst) -> ten sam
`handle_message`. Zależność: ffmpeg. Pokazać transkrypcję nad odpowiedzią,
żeby błąd rozpoznania był widoczny ("ławka 4x8 80" vs "4x8 18").

**Dashboard:** FastAPI `GET /dash` (za Tailscale, bez auth poza siecią) -
jeden plik HTML, wykresy z danych przez `GET /api/series?metric=...`
(waga 7d, HRV z bazą, km/tydz., bilans, zdjęcia obok siebie). Bez
frameworka; odświeżanie ręczne.

**Backup poza VPS:** po `backup_database` kopiować zaszyfrowany dump do
prywatnego storage'u obiektowego przez `restic`/`rclone` albo przez `scp`
na drugi node Tailscale. Poświadczenia trzymać poza repo; monitorować ostatnią
udaną kopię i okresowo sprawdzać restore.

**VPS (Faza 6, WDROŻONE):** `Dockerfile` (uv, non-root), usługi `api` i
`bot` w Compose z `restart: unless-stopped`, reverse proxy HTTPS, webhook
i autostart działają. Szczegóły hosta i procedura operacyjna są w ignorowanym
`VPS_LOCAL.md`; repozytorium zawiera przenośne `deploy/install.sh`,
`deploy/update.sh` i `deploy/doctor.sh`.
