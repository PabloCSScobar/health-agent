# health-agent

Osobista aplikacja do zbierania danych zdrowotnych i treningowych oraz
rozmowy o nich z agentami LLM po polsku. Łączy Intervals.icu (dane z Suunto),
Health Connect (m.in. Fitatu i Fitdays), ręczne wpisy i import notatek.
Głównym interfejsem jest Telegram; CLI służy do rozmowy i operacji ręcznych.

## Orientacja w projekcie

- [AGENTS.md](AGENTS.md) — wspólne zasady pracy agentów programistycznych.
- [CLAUDE.md](CLAUDE.md) — import tych samych zasad dla Claude Code.
- [Workflow Codex](docs/codex-workflow.md) — Terra, konsultacje Astry i niezależna weryfikacja.
- [Architektura](docs/architecture.md) — mapa kodu, przepływ danych i pułapki.
- [TODO.md](TODO.md) — backlog i stan funkcji.
- [PLAN.md](PLAN.md) — kolejność i szczegóły planowanych implementacji.
- [scripts/README.md](scripts/README.md) — historyczne eksperymenty i decyzje;
  stare statusy i ścieżki trzeba konfrontować z kodem.

Instrukcje są wersjonowanym kontekstem projektu, a nie kopią historii czatu.
Codex korzysta z `AGENTS.md`; Claude Code ładuje go przez `@AGENTS.md`
w `CLAUDE.md`. Szczegółowe dokumenty agenci czytają stosownie do zadania.
Po zmianie sposobu uruchamiania, architektury lub statusu funkcji agent ma
aktualizować odpowiedni dokument w tej samej zmianie. Nie wymaga to
osobnego procesu synchronizacji między narzędziami.

## Środowisko

Linux/WSL, Python >=3.12, `uv`, Docker z Compose; PostgreSQL 16.
Repo korzysta z układu `src/` i pliku `uv.lock`.
Poniższe komendy wykonuj w Bash z katalogu głównego repo.

W WSL polecenia linuksowe można uruchamiać również z PowerShell, wskazując
ścieżkę do sklonowanego repozytorium:

```powershell
wsl.exe -d Ubuntu-24.04 --cd /home/<uzytkownik>/health-agent -- uv run health-agent --help
```

## Pierwsze przygotowanie

Rekomendowany wariant uruchamia bazę, migracje, API z jednym workerem oraz
bota w Docker Compose. Lokalny development:

```bash
./deploy/install.sh
```

Pierwsza instalacja na VPS:

```bash
./deploy/install.sh production
```

Skrypt tworzy nowy `.env`, generuje hasło bazy i sekret webhooka, a następnie
zatrzymuje się do czasu wpisania tokenów. Szczegóły instalacji, aktualizacji,
Tailscale i migracji istniejącej bazy opisuje [deploy/README.md](deploy/README.md).

Do pracy natywnej bez konteneryzacji aplikacji:

```bash
uv sync --frozen
cp -n .env.example .env
```

Uzupełnij `.env` lokalnie. Pełny zestaw ustawień definiuje
`src/health_agent/settings.py`, a `.env.example` jest szablonem wdrożeniowym.

- `DATABASE_URL` — adres SQLAlchemy, np.
  `postgresql+psycopg://health_agent:health_agent_dev@localhost:5432/health_agent`.
  To domyślna konfiguracja lokalna, nie hasło do publicznego wdrożenia.
  Jeśli ustawiasz `POSTGRES_PASSWORD` dla Compose, dopasuj też `DATABASE_URL`.
- `ANTHROPIC_API_KEY` — dla modeli Anthropic; wybór per agent znajduje się
  w `config/agents.yaml`. Ollama jest alternatywą skonfigurowaną w `Settings`.
- `APP_ENV` — `development`, `production` albo `test`; `/status` bota pokazuje
  aktywne środowisko.
- `SCHEDULER_ENABLED` — uruchamia scheduler wewnątrz API. Lokalnie domyślnie
  `false`, na VPS ustawiane przez `install.sh production` na `true`.
- `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID` — polling treningów i wellness.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — bot i dozwolony czat. Development
  i production muszą mieć osobne tokeny botów, jeśli działają równocześnie.
- `WEBHOOK_SHARED_SECRET` — wspólna wartość z nagłówkiem `X-Webhook-Secret`
  w aplikacji telefonu. Ustaw przed udostępnieniem webhooka.
- `DASHBOARD_PASSWORD_HASH` — hash Argon2id generowany przez
  `health-agent hash-password`; w pliku `.env` ujmij go w pojedyncze
  cudzysłowy, aby znaki `$` pozostały literalne.
- `DASHBOARD_ACCESS_MODE` i `DASHBOARD_URL` — sposób dostępu raportowany przez
  `/dashboard`: `local`, `tailscale`, `public` albo `disabled`. Opcjonalne
  `DASHBOARD_ALLOWED_IPS` przyjmuje adresy i CIDR rozdzielone przecinkami;
  `DASHBOARD_TRUSTED_PROXIES` określa, od których proxy wolno ufać
  `X-Forwarded-For`. Tryb nie konfiguruje sieci ani reverse proxy: wystawienie
  adresu nadal wymaga osobnego Tailscale Serve albo Caddy.
- `BACKUP_*`, `DAILY_SUMMARY_*`, `WEEKLY_SUMMARY_*`, `CORRELATIONS_*`,
  `REMINDERS_*` — opcje schedulera; raporty i korelacje są
  domyślnie wyłączone, przypomnienia są włączone, ale działają dopiero po
  utworzeniu i potwierdzeniu reguły.
- `FITATU_*` — obecnie dla eksperymentalnego skryptu API, nie głównej ingestii.

W natywnym trybie deweloperskim można uruchomić tylko bazę i zastosować migracje:

```bash
docker compose up -d db
uv run alembic upgrade head
```

Pełne `docker compose up -d` uruchamia także migrację, API, serwer MCP i bota.
Porty bazy, API i MCP są publikowane wyłącznie na `127.0.0.1`; dostęp HTTPS zapewnia Tailscale
działający na hoście.

## Serwer MCP i przełącznik czatu Telegrama

Aplikacja może działać jako sama warstwa danych, a rozmowę prowadzi agent
„Zdrowie” w OpenBot (Claude Code). Serwer MCP (`src/health_agent/mcp/server.py`,
nazwa `health`, streamable HTTP, bezstanowy, odpowiedzi JSON) udostępnia
narzędzia z `tools/` pod tymi samymi nazwami, co dawniej agenci LLM, oraz
kilka nowych. Nie ma w nim routingu (`delegate`, `ask_agent`). Proces nie
uruchamia schedulera ani bota.

Narzędzia do odczytu: `get_running_profile`, `get_weekly_running_load`,
`get_intensity_distribution`, `get_efficiency_trend`, `get_fitness_form`,
`analyze_run`, `find_comparable_runs`, `get_latest_workout`,
`get_workouts_on_date`, `get_workouts`, `get_correlations`,
`get_strength_sessions`, `get_exercise_progress`, `get_strength_weekly_volume`,
`get_nutrition_summary`, `get_energy_balance`, `find_foods`, `get_nutrition_day`,
`get_nutrition_range`, `get_body_trend`, `get_body_composition_latest`,
`get_body_composition_trend`, `get_recovery_baseline`, `get_recovery_day`,
`get_recovery_range`, `get_wellbeing_history`, `estimate_daily_expenditure`,
`get_knowledge`, `list_documents`, `read_document`, `get_user_profile`,
`list_supplements`, `list_reminder_rules`, `get_data_freshness`.

Narzędzia zapisujące: `log_manual_entries`, `set_user_profile_facts`,
`remember_fact`, `forget_knowledge`, `restore_knowledge`, `add_supplement`,
`record_supplement_intake`, `propose_reminder_rule` (tylko szkic),
`activate_reminder_rule` (wyłącznie po jawnym potwierdzeniu szkicu przez
użytkownika) i `save_document` (oryginał notatki do `documents` z deduplikacją
sha256, bez LLM i bez ekstrakcji faktów).

Zmienne środowiskowe (`.env`):

- `MCP_TOKEN` — wymagany sekret; klient wysyła `Authorization: Bearer <token>`.
  Pusty token oznacza odrzucanie każdego żądania (401) i ostrzeżenie w logu.
  Wygeneruj np. `openssl rand -hex 32`.
- `MCP_ALLOWED_HOSTS` — lista nagłówków `Host` akceptowanych przez ochronę
  przed DNS rebinding, rozdzielona przecinkami. Domyślnie
  `health-agent-vps.tail8242a2.ts.net,127.0.0.1:8765,localhost:8765`.
- `MCP_PORT` — port publikowany na `127.0.0.1` hosta (domyślnie 8765).
- `TELEGRAM_CHAT_MODE` — `agents` (domyślnie: czat obsługują agenci tej
  aplikacji) albo `openbot`.

Uruchomienie lokalne bez Dockera:

```bash
MCP_TOKEN=lokalny-token uv run uvicorn health_agent.mcp.server:app --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/health   # {"status":"ok"} bez tokenu
```

Wystawienie w tailnecie (na hoście VPS; serwer słucha tylko na `127.0.0.1`):

```bash
sudo tailscale serve --bg --https=443 --set-path=/mcp http://127.0.0.1:8765/mcp
```

Połączenie z OpenBot / Claude Code (token z `.env` serwera):

```bash
claude mcp add --transport http health https://health-agent-vps.tail8242a2.ts.net/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

Odpowiednik w `.mcp.json`: `{"mcpServers": {"health": {"type": "http", "url":
"https://health-agent-vps.tail8242a2.ts.net/mcp", "headers": {"Authorization":
"Bearer ${MCP_TOKEN}"}}}}`.

Przy `TELEGRAM_CHAT_MODE=openbot` bot nie wywołuje żadnego LLM: wiadomości
tekstowe, import plików oraz `/daily` i `/weekly` dostają jedną linię z
informacją, że czat przeniesiono do OpenBot, a automatyczne podsumowania
schedulera są pomijane. Bez zmian działają przypomnienia z przyciskami,
reakcje (feedback), zdjęcia do archiwum oraz `/status`, `/sync`, `/foto`,
`/suple`, `/przypomnienia`, `/dashboard`, `/profil` (tylko podgląd) i `/cost`.

## Natywne uruchamianie aplikacji

API z webhookiem i schedulerem, lokalny HTTP:

```bash
uv run uvicorn health_agent.api.app:app --host 127.0.0.1 --port 8000
```

Wariant HTTPS z telefonem przez Tailscale wymaga własnego certyfikatu
i klucza w `secrets/` oraz działającej konfiguracji sieci:

```bash
uv run uvicorn health_agent.api.app:app --host 0.0.0.0 --port 8000 \
  --ssl-certfile secrets/tailscale.crt --ssl-keyfile secrets/tailscale.key
```

API udostępnia `GET /health`, `POST /webhook/healthconnect` oraz
uwierzytelniony dashboard `GET /dash`. Dashboard ma sesje w PostgreSQL,
ochronę CSRF/Origin, limit prób logowania i prywatne endpointy zdjęć.
Frontend to pliki statyczne z pakietu (`src/health_agent/api/static/`)
serwowane wyłącznie pod `/dash/static/`, bez zewnętrznych fontów, CDN
i skryptów inline (CSP `script-src 'self'`). Sekcje: Przegląd (kafelki,
wykresy SVG 7/30/90 dni, ostatnie treningi, świeżość źródeł), Korelacje,
Zdjęcia (filtr ujęcia i porównanie dwóch zdjęć), Suplementy, Przypomnienia
(szkice, edycja, historia wysyłek) oraz Proaktywne alerty. Adres `#sekcja`
w URL zapamiętuje otwartą zakładkę. Interfejs wspiera tryb ciemny systemu
i `prefers-reduced-motion`; usuwanie zdjęcia i ponowna wysyłka wymagają
potwierdzenia.
Sekcja „Proaktywne alerty” pokazuje ocenę trzech deterministycznych reguł,
historię wysyłek i przełączniki tematów. Wszystkie są domyślnie wyłączone;
wiadomość zbiorcza jest oceniana o 20:00 Europe/Warsaw przez istniejący outbox.
W production webhook odmawia pracy, jeśli `WEBHOOK_SHARED_SECRET` nie jest
ustawiony.
`/health` sprawdza odpowiedź procesu, nie bazę ani świeżość danych.
Gdy `SCHEDULER_ENABLED=true`, lifespan uruchamia polling Intervals.icu,
alerty i domyślnie backup. Uruchamiaj wtedy jedną instancję API; wiele
workerów powieli scheduler. Wyłączenie alertów i backupu nie wyłącza pollingu.
Przy `SCHEDULER_ENABLED=false` API i webhook działają bez zadań okresowych.

W osobnym terminalu bot (long polling):

```bash
uv run python -m health_agent.channels.telegram
```

Obsługuje tekst, zdjęcia sylwetki, import dokumentów tekstowych oraz
`/status`, `/cost`, `/profil`, `/sync`, `/daily`, `/weekly`,
`/foto`, `/suple`, `/przypomnienia` i `/dashboard`. Ostatnia komenda podaje
skonfigurowany adres oraz informuje, czy wymagane jest połączenie Tailscale.
`/sync` uruchamia ten sam
bezpieczny catch-up Intervals.icu co scheduler; nie odświeża jeszcze żywienia
z Fitatu. `/status` pokazuje również środowisko i stan
schedulera. Reakcje 👍/👎 na odpowiedzi zapisują feedback; komentarz można
dodać jako reply zaczynający się od 👍, 👎 albo `feedback:`. Nie uruchamiaj
dwóch botów z tym samym tokenem; utwórz osobny bot przez BotFather dla dev.
Podpis zdjęcia ma format np. `przód 2026-09-19 rano`; `/foto przód`
pokazuje ostatnie ujęcia z wagą i procentem tłuszczu. Zdjęcia nie są
udostępniane agentom. Prośba o przypomnienie tworzy szkic i wymaga kliknięcia
`Aktywuj`; wysłane przypomnienia mają przyciski wykonania, pominięcia i
odroczenia o 30 minut.

Przy aktualizacji starszego lokalnego `.env`, który nie ma jeszcze
`APP_ENV`, najpierw utwórz osobnego bota dev i wpisz jego token, a następnie
dodaj:

```env
APP_ENV=development
SCHEDULER_ENABLED=false
TELEGRAM_BOT_TOKEN=<token-osobnego-bota-dev>
```

Dopiero potem uruchamiaj lokalny `bot` lub pełne Compose. Instalator wymaga
jawnego argumentu, jeśli zastanie starszy `.env` bez `APP_ENV`.

Przykłady CLI (chat/import wywołują modele i zapisują dane; ingest zapisuje do bazy):

```bash
uv run health-agent --help
uv run health-agent chat "Podsumuj ostatni tydzień biegania"
uv run health-agent daily
uv run health-agent weekly
uv run health-agent feedback --bad --days 30
uv run health-agent correlations
uv run health-agent correlations --publish
uv run health-agent hash-password
uv run health-agent ingest intervals --since 2026-09-01 --until 2026-09-07
uv run health-agent ingest healthconnect --file /tmp/synthetic-healthconnect.json
uv run health-agent import /tmp/synthetic-note.md --title "Notatka testowa" --date 2026-09-01
uv run health-agent knowledge --domain running
```

Ścieżki `/tmp/synthetic-*` są przykładami, nie plikami dostarczonymi w repo.
`undo-import ID` modyfikuje dokumenty/wiedzę i nie cofa zmian w profilu.
Argumenty sprawdzaj w `--help`; import nie ma flagi `--domena`.

## Weryfikacja zmian

Kontrole składni i parsera CLI bez połączeń z usługami aplikacji,
po wcześniejszym `uv sync --frozen`:

```bash
uv run --frozen --no-sync python -m compileall -q src scripts alembic
uv run --frozen --no-sync python -m unittest discover -s tests -p 'test_*.py'
uv run --frozen --no-sync health-agent --help
git diff --check
```

Testy w `tests/` używają standardowego `unittest`. Repo nie ma obecnie
konfiguracji `pytest` ani CI. `scripts/test_*.py` to smoke testy integracji.

Rozszerzony pakiet funkcji dashboard/zdjęcia/przypomnienia uruchamia własny,
jednorazowy PostgreSQL 16, sprawdza współbieżność outboxa, recovery po błędach,
sesje i mock Telegrama, a na końcu wykonuje prawdziwy backup i restore:

```bash
bash scripts/run_feature_integration_tests.sh
```

Skrypt wymaga działającego Dockera, używa wyłącznie danych syntetycznych i
zatrzymuje testowy kontener przez `trap` również po nieudanym teście.
Destrukcyjne cleanupy testów są dodatkowo blokowane, dopóki baza i użytkownik
nie nazywają się `health_test` oraz runner nie poda jawnego tokenu testowego.

`scripts/eval_agents.py` wymaga modeli i bazy, generuje koszt API,
może zmieniać profil/wiedzę i czyści **całe `agent_runs`**. Uruchamiaj go
wyłącznie z `DATABASE_URL` wskazującym osobną bazę testową z migracjami
i odpowiednimi danymi syntetycznymi. Sam inny branch/worktree nie izoluje
bazy. Nie ma gotowego skryptu przygotowującego pełne środowisko testowe.

Po ustawieniu testowego `DATABASE_URL` wybierz adekwatny zestaw:

```bash
uv run python scripts/eval_agents.py core
uv run python scripts/eval_agents.py running
uv run python scripts/eval_agents.py coaches
uv run python scripts/eval_agents.py import
```

Dostępne są też pojedyncze `recovery`, `nutrition`, `body`, `strength`.
Tryb `all` obejmuje obecnie tylko core + running. Sędzia LLM ocenia kontrakt
odpowiedzi, nie stanowi dowodu poprawności wszystkich liczb.

## Dane i utrzymanie

`.env`, `.venv/`, `secrets/`, `backups/` i `data/` są ignorowane przez Git.
Nie umieszczaj danych zdrowotnych ani kluczy w dokumentacji i fixture'ach.
Domyślne modele aplikacji korzystają z Anthropic: treść pytań i kontekst
przekazany modelom mogą opuszczać komputer. Planowane zdjęcia sylwetki
mają pozostać poza analizą LLM zgodnie z decyzją użytkownika.

Backup w schedulerze robi `pg_dump` bazy wskazanej przez `DATABASE_URL`
oraz osobne archiwum zdjęć z manifestem SHA-256 (domyślnie co 24h,
retencja 14 dni, także przy starcie). W Compose zapisuje pliki w nazwanym
wolumenie `health_agent_backups`; zdjęcia robocze są w osobnym,
współdzielonym wolumenie `health_agent_photos`.
Backup na tym samym hoście nie jest kopią poza VPS-em.

Przed przekazaniem pracy między Codex i Claude Code zatrzymaj edycję w jednym
narzędziu, przejrzyj diff i przekaż cel oraz wynik weryfikacji. Przy dłuższym
przerwanym zadaniu zostaw notatkę w odpowiednim punkcie `TODO.md`.
Do równoległej pracy używaj oddzielnych checkoutów; procesy i bazę trzeba
izolować niezależnie.

## Licencja

Projekt jest udostępniany na licencji MIT. Szczegóły znajdują się w pliku
[LICENSE](LICENSE).
