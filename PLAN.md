# Plan implementacji - kolejne funkcje (2026-09-18)

Kolejność wynika z zależności i z tego, co już nas ugryzło. Każdy punkt ma:
cel, decyzje (rozstrzygnięte na podstawie sprawdzonych faktów), kroki,
pliki, weryfikację. Szacunki kosztu = tokeny/API, nie "dni pracy". Stan
tła: `TODO.md` (co i dlaczego), `scripts/README.md` (historia decyzji).

Fakty sprawdzone przed planem (nie założenia):
- WSL ma systemd (`/etc/wsl.conf: systemd=true`, PID 1 = systemd),
  `tailscaled` już działa jako usługa systemd -> autostart przez unity
  systemd, bez Dockerfile. Docker Desktop (Windows) podnosi dystrybucję i
  kontener bazy przy logowaniu - to dlatego baza "wstawała sama".
- python-telegram-bot 22.8: jest `MessageReactionHandler`; reakcje
  przychodzą tylko gdy `run_polling(allowed_updates=...)` zawiera
  `message_reaction`.
- Biblioteka Fitatu: `login(email, password)`, `refresh_access_token`,
  `get_day_plan(date)` - refresh flow jest, brakuje tylko poświadczeń.
- Brak `Dockerfile` - konteneryzacja aplikacji zostaje na Fazę 6.

---

## 1. Autostart po restarcie WSL (PILNE)

**Cel:** po restarcie Windows/WSL webhook, scheduler i bot wstają same;
padnięty proces jest restartowany.

**Decyzja:** systemd (system-level, `User=pawel`), nie Docker i nie
`--user` (user-units wymagają lingera i sesji; system-units startują z
dystrybucją niezależnie od logowania do WSL). Docker zostaje dla bazy.

**Kroki:**
1. `deploy/health-agent-api.service` i `deploy/health-agent-bot.service`:
   `WorkingDirectory=/home/pawel/projects/health_agent`, `ExecStart=uv run
   uvicorn ...` (pełna ścieżka do `uv`: `which uv`), `Restart=always`,
   `RestartSec=10`, `After=network-online.target docker.service
   tailscaled.service`, `Wants=network-online.target`. Logi do journald
   (`journalctl -u health-agent-api -f`).
2. `deploy/wait_for_db.sh` jako `ExecStartPre`: pętla `pg_isready` przez
   `docker compose exec db` (max 120 s) - Docker Desktop podnosi bazę z
   opóźnieniem względem systemd w WSL; bez tego pierwszy poll/webhook po
   starcie wysypie się na połączeniu.
3. `deploy/install.sh`: kopiuje unity do `/etc/systemd/system/`,
   `daemon-reload`, `enable --now`. Jednorazowo `sudo`.
4. Usunąć z README/notatek instrukcje `nohup`; `scripts/README.md` -
   wpis "uruchamianie = systemd".
5. **Sprawdzić, czy `tailscaled` wstaje przed API** - inaczej cert/adres
   MagicDNS niedostępne przez chwilę; `After=tailscaled.service` załatwia
   kolejność, `Restart=always` załatwia wyścig.

**Weryfikacja:** `sudo systemctl restart health-agent-api` -> `/health`
odpowiada; `kill -9` procesu -> wraca w 10 s; `wsl --shutdown` z Windows,
ponowne wejście -> oba serwisy `active`, webhook z telefonu ("Sync Now")
ląduje w `raw_payloads`, bot odpowiada na Telegramie. Test negatywny: bez
działającej bazy `ExecStartPre` czeka, nie startuje aplikacji na ślepo.

**Ryzyko:** WSL nie startuje bez uruchomienia Docker Desktop / okna WSL po
zalogowaniu do Windows - to poza systemd; jeśli Docker Desktop ma "start at
login" (sprawdzić), dystrybucja z integracją wstaje razem z nim. Jeśli nie:
Task Scheduler w Windows z `wsl.exe -d <distro> -- true` przy logowaniu
(jedna linia, dopisać do `deploy/README`).

---

## 2. Pętla zwrotna 👍/👎 z Telegrama

**Cel:** każda odpowiedź asystenta ma przypięty `agent_run` (korzeń
drzewa); reakcja 👍/👎 pod wiadomością zapisuje ocenę; `health-agent
feedback` pokazuje 👎 z pytaniem, odpowiedzią, agentem i narzędziami - to
jest materiał do poprawiania promptów zamiast zgadywania.

**Decyzje:** reakcje (nie przyciski inline) - zero tarcia, natywne w
Telegramie. Komentarz tekstowy opcjonalnie: odpowiedź (reply) na wiadomość
bota zaczynająca się od "👎"/"👍" albo "feedback:" zapisuje tekst do tej
samej oceny.

**Kroki:**
1. Migracja: `conversations.telegram_message_id` (int, index),
   `conversations.root_run_id` (FK agent_runs); tabela `feedback` (id,
   conversation_id, agent_run_id, rating -1/+1, comment, created_at).
2. `run_agent` zwraca dziś tylko output - dodać sposób odczytu id korzenia:
   contextvar `_last_root_run_id` ustawiany w `run_agent`, gdy
   `parent_run_id is None`; `ask_orchestrator_async` zwraca `(answer,
   root_run_id)` (albo nowa funkcja, żeby nie łamać CLI/eval).
3. `telegram.py`: `_reply` zwraca `Message` (pierwszy chunk) -> zapis
   `telegram_message_id` + `root_run_id` w wierszu asystenta.
   `MessageReactionHandler(handle_reaction)`: z `update.message_reaction`
   bierze `message_id`, `new_reaction` (ReactionTypeEmoji: 👍/👎), szuka
   wiersza po `telegram_message_id`, upsert `feedback`. Reply-to z tekstem
   -> `comment`. `run_polling(allowed_updates=Update.ALL_TYPES)`.
4. CLI `health-agent feedback [--days 30] [--bad]`: lista ocen z
   pytaniem/odpowiedzią/agentem/narzędziami/kosztem; `--export plik.md` do
   przeglądu.
5. Deterministyczne wykorzystanie: `check_stale_sources`-style job NIE -
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

## 4. `/sync` + dzisiejsze jedzenie z Fitatu

**Cel:** "ile zjadłem dziś" działa zanim telefon zsynchronizuje Health
Connect; `/sync` odświeża Intervals.icu + Fitatu na żądanie.

**Decyzje (kluczowe - dedup źródeł):** pozycje z API Fitatu to źródło
TYMCZASOWE (`source="fitatu_api"`). Health Connect jest kanoniczne. Reguły:
(a) ingest Fitatu dla dnia = usuń wszystkie `fitatu_api` z tego dnia,
wstaw na nowo (cały dzień, nie upsert per pozycja - API daje pełny dzień);
(b) ingest Health Connect dla dnia = usuń `fitatu_api` z tego dnia;
(c) `_recompute_nutrition_day` sumuje TYLKO jedno źródło: healthconnect
jeśli ma pozycje tego dnia, inaczej fitatu_api. Bez (c) dzień liczy się
podwójnie w oknie między syncami - to ten sam typ błędu co duplikaty z
15.09, tylko z dwóch źródeł.

**Kroki:**
1. Settings: `FITATU_EMAIL`, `FITATU_PASSWORD`; `ingest/fitatu.py`:
   klient z `login` + cache tokenu w `ingest_state`-podobnej tabelce lub
   pliku w `secrets/` (token 60 min, refresh przez bibliotekę);
   `fetch_day(date)` -> normalizacja (te same pola co HC: product, kcal,
   protein_g, fat_g, carbs_g, fiber_g, sugar_g, salt_g; `meal` - tu JEST
   typ posiłku, zapisać) -> reguła (a) -> recompute.
2. `ingest/healthconnect.py`: reguła (b) przed upsertem pozycji dnia;
   `_recompute_nutrition_day`: reguła (c).
3. Narzędzie `refresh_today_nutrition()` dla nutrition (i przez ask_agent
   dla innych): wołane tylko gdy pytanie dotyczy DZIŚ i `get_nutrition_day`
   dziś jest puste/ubogie; zwraca podsumowanie + informację "z API Fitatu,
   Health Connect nadpisze po syncu".
4. Telegram `/sync`: Intervals (ostatnie 3 dni) + Fitatu (dziś, wczoraj)
   + wynik jednym komunikatem; CLI `health-agent ingest fitatu --date`.
5. `scripts/test_fitatu.py` -> logika do `ingest/fitatu.py`, skrypt
   zostaje jako smoke test.

**Weryfikacja:** dzień bez HC: `/sync` -> nutrition_days z sumą z Fitatu;
potem replay payloadu HC dla tego dnia -> fitatu_api znika, suma = HC;
ponowny `/sync` tego samego dnia -> ta sama liczba pozycji (nie x2);
`eval_agents.py nutrition` bez regresji + nowy test jednostkowy na (a)-(c)
z syntetycznymi pozycjami.

**Ryzyko:** nieoficjalne API (ToS, patrz README pkt 9) - używać tylko na
żądanie, z nagłówkami jak przeglądarka (już zrobione w skrypcie), nigdy w
schedulerze. Hasło w `.env` - ten sam poziom co dziś token.

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

## 6. Tygodniowe podsumowanie (niedziela wieczorem)

**Cel:** jedna wiadomość: km/obciążenie i forma, regeneracja, dieta i
bilans, waga - co poszło dobrze, co poprawić, jedna rzecz na następny
tydzień. Bez dziennego szumu (odrzucony).

**Decyzja:** 4 specjalistów równolegle (`asyncio.gather`) z tym samym
pytaniem-szablonem ("Podsumuj tydzień pon-nd: 3-4 liczby, 1 zdanie oceny
względem poprzedniego tygodnia, 1 rekomendacja") - bez orchestratora
(delegowałby do jednego). Modele wg configu (running Sonnet/Haiku po pkt
3). Sklejone z nagłówkami, do 2 wiadomości Telegram. Zapis jako
`Conversation(agent="weekly")`, żeby "a dlaczego waga?" miało kontekst.

**Kroki:** `agents/weekly.py` (`build_weekly_summary()`), job cron
`day_of_week=sun, hour=20` w strefie Europe/Warsaw (scheduler jest UTC -
podać `timezone` w triggerze), settings `WEEKLY_SUMMARY_ENABLED/DAY/HOUR`,
CLI `health-agent weekly` (podgląd bez wysyłki), reguła w promptach
"tryb podsumowania: bez ❓, bez ofert pogłębienia".

**Weryfikacja:** CLI generuje 4 sekcje z liczbami z narzędzi (sprawdzić 2
liczby ręcznie w bazie); brak danych w domenie (np. siłownia) -> sekcja
"brak wpisów", nie zmyślona; sędzia (tryb analysis) >= 4/5 na każdej
sekcji; koszt jednego podsumowania zmierzony w `agent_runs` (oczekiwane
$0.10-0.15).

---

## Zależności i kolejność

1 (autostart) -> 2 (feedback; migracja `conversations`) -> 3 (Haiku;
niezależne, trywialne) -> 4 (`/sync`; niezależne) -> 5 (korelacje) -> 6
(tygodniowe; korzysta z lekcji z 5 przez digest). 3 i 4 można wcisnąć w
dowolnym miejscu. Po każdym punkcie: `eval_agents.py` (core + dotknięty
zestaw), commit.

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

**Backup poza dyskiem PC:** najtańsze bez nowej infrastruktury - po
`backup_database` skopiować plik do katalogu synchronizowanego przez
Windows (OneDrive/Google Drive pod `/mnt/c/Users/.../`), ścieżka w
settings `BACKUP_MIRROR_DIR`. Alternatywa: `scp` na drugi node Tailscale.

**VPS/RPi (Faza 6):** `Dockerfile` (uv, non-root), usługi `api` i `bot` w
compose z `restart: unless-stopped`, `secrets/` jako volume, Tailscale na
hoście, migracja bazy `pg_dump | psql`, zmiana URL w apce Health Connect
Webhook. Unity z pkt 1 przestają być potrzebne - dlatego pkt 1 celowo nie
wchodzi w Dockerfile.
