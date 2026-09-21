# Mapa architektury health-agent

Zweryfikowana przez odczyt kodu 2026-09-19; nie jest potwierdzeniem stanu
uruchomionych usług ani danych w bazie. Przy zmianach aktualizuj opis
odpowiedniej części, a plany utrzymuj w `TODO.md` i `PLAN.md`.

## Przepływ danych

1. Intervals.icu (treningi i wellness z Suunto) jest odpytywane przez
   `ingest/intervals.py`. Scheduler, Telegram `/sync` i CLI korzystają ze
   wspólnej orkiestracji `ingest/sync.py`.
2. Telefon wysyła Health Connect do `api/app.py`, który przekazuje payload
   do `ingest/healthconnect.py`: skład ciała, żywienie i aktywność dzienna.
3. Ingestory zapisują surowe payloady do audytu oraz upsertują dane
   znormalizowane w PostgreSQL. Powtórny import ma być idempotentny dla
   danych znormalizowanych; kolejne wpisy audytu są spodziewane.
4. Telegram/CLI przekazuje pytanie do orchestratora, który deleguje do
   specjalisty. Narzędzia Pythona czytają dane, liczą metryki i zwracają
   wyniki do interpretacji. Historia Telegrama trafia do bazy.
5. Import `.txt`/`.md` przebiega osobno: model importera ekstrahuje i
   rekoncyliuje fakty, zapisuje oryginał oraz wiedzę, ewentualnie profil.

Ścieżki modułów poniżej odnoszą się do `src/health_agent/`, jeśli nie
zaznaczono inaczej.

## Gdzie wprowadzać zmiany

- `settings.py`: ustawienia i `.env`, w tym `APP_ENV` oraz przełącznik
  schedulera; `settings` powstaje przy imporcie.
- `db/models.py`: tabele i ograniczenia; `db/session.py`: sesja z commit/rollback.
  Zmiany schematu mają migracje w katalogu repo `alembic/versions/`.
- `api/app.py`: FastAPI, autoryzacja webhooka, start/stop schedulera.
- `api/dashboard.py`: sesje Argon2id/CSRF, dashboard i prywatne API danych,
  zdjęć, suplementów oraz przypomnień. Frontend leży w `api/static/`
  (`dashboard.html/.css/.js`, `login.html/.js`); strony HTML są wczytywane
  przy imporcie z wersją zasobów w query (`?v=<hash>`), a `/dash/static/{nazwa}`
  serwuje tylko pliki z allowlisty `STATIC_ASSETS`. Interfejs buduje DOM
  z danych API wyłącznie przez bezpieczne węzły i `textContent`, wykresy to
  inline SVG bez bibliotek.
- `tools/overview.py`: agregaty zakładki Przegląd (ostatnia wartość, średnia,
  porównanie z poprzednim oknem, treningi, świeżość źródeł) liczone z
  `daily_frame`; brak pomiaru pozostaje `None`. Zmiana średniej jest pokazywana
  tylko wtedy, gdy oba okresy mają co najmniej dwa dni z danymi, a liczebność
  próbek nie różni się więcej niż dwukrotnie.
- `scheduler.py`: polling, alerty na Telegram i backup; transakcyjny catch-up
  Intervals.icu oraz blokada między procesami są w `ingest/sync.py`.
- `ingest/`: normalizacja źródeł i dedup, bez generowania odpowiedzi LLM.
- `tools/`: narzędzia domenowe oraz zapis wpisów ręcznych, profilu i wiedzy.
- `tools/correlations.py`: dzienny frame i jawne pary Spearmana;
  `tools/photos.py`: lokalny magazyn zdjęć bez dostępu LLM;
  `tools/reminders.py`: suplementy, reguły, świeżość danych i outbox.
- `agents/base.py`: fabryka modeli, prompt caching, async run, rejestracja
  kosztu/czasu/narzędzi i drzewa wywołań w `agent_runs` przez `ContextVar`.
- `agents/registry.py`: routing, narzędzia, onboarding, wspólny kontrakt
  specjalistów i dołączanie profilu/digestu wiedzy.
- `prompts/{running,strength,nutrition,body,recovery}.md`: metodologia domenowa.
- `agents/importer.py`: ekstrakcja, rekoncyliacja, raport i cofnięcie importu.
- `channels/telegram.py`: autoryzacja czatu, tekst/dokumenty, historia, komendy
  i reakcje feedbacku. `/dashboard` czyta tryb i adres z konfiguracji; nie
  wykrywa dostępności sieci ani nie przełącza reverse proxy.
- `agents/summaries.py`: równoległe raporty daily/weekly z czterech specjalistów, spięte wspólnym korzeniem `agent_runs`.
- `cli/main.py`: parser komend i wywołanie istniejących funkcji aplikacji.
- `config/agents.yaml` w repo: wybór modeli per agent, wczytywany przez
  `agents/base.py` przy imporcie; zmiana wymaga restartu procesu.

## Agenci aplikacji

Orchestrator kieruje do `running`, `strength`, `nutrition`, `body`,
`recovery`. Delegacja i batch wpisów ręcznych są output functions: wynik funkcji
jest finalną odpowiedzią bez dodatkowej parafrazy routera. Zapis wielu
faktów profilowych jest zwykłym narzędziem, żeby nie gubić kolejnych zapisów.
Batch jest walidowany w całości i zapisuje wszystkie skutki w jednej transakcji.

Specjalista może konsultować drugiego przez `ask_agent`; konsultowany
leaf agent nie ma tego narzędzia, co ogranicza rekurencję. Wywołania
zagnieżdżone są asynchroniczne. CLI ma wejście synchroniczne, Telegram
korzysta z async, bez zagnieżdżania `asyncio.run`.

Agenci otrzymują datę, profil i ograniczony digest wiedzy. Obliczenia są
w narzędziach, odpowiedzi mają bazować na zwróconych liczbach. Modele
konfiguruje YAML; nie kopiuj zmiennych nazw modeli do instrukcji.

## Dane i pamięć

- Pomiary: `workouts`, `daily_activity`, `sleep`, `recovery`,
  `body_composition`, `nutrition_days`, `nutrition_items`.
- `raw_payloads`: audyt/replay; `ingest_state`: ostatni udany sync źródła,
  aktualizowany w transakcji z ingestem.
- `manual_logs`: ręczne wpisy i oryginalny tekst; atomowy batch i dedup są w
  `tools/manual_batch.py`, a `tools/manual.py` zachowuje zgodność pojedynczego
  wpisu. Recovery czyta historię `wellbeing` (1-5) i `note`.
- `agent_memory`: profil pod `user_profile`, dedup podsumowań pod
  `system_summaries` i historyczne znaczniki wycofanego alertu pod
  `system_alerts`. Komentarze sugerujące wyłącznie profil są niepełne.
- `documents`: oryginały importowanych notatek, data, skrót i hash dedup.
- `knowledge`: fakty/wnioski z domeną, pochodzeniem, datą, pewnością,
  aktywnością i `superseded_by`; digest plus narzędzia odczytu szczegółów.
- `conversations`: historia czatu oraz Telegram `message_id` (wszystkie części) i root run odpowiedzi.
  `feedback`: jedna aktualna ocena 👍/👎 z opcjonalnym komentarzem na odpowiedź.
  `agent_runs`: model, tokeny, koszt, czas, nazwy narzędzi i rodzic wywołania;
  raport wieloagentowy ma syntetyczny korzeń `parallel-specialists`.
- `correlation_results`: wersjonowane wyniki pięciu jawnych par;
  kwalifikujące obserwacje mają aktywny wpis `knowledge` rodzaju `lekcja`
  z niską pewnością.
- `progress_photos`: tylko metadane i stan pliku; bajty są na współdzielonym
  wolumenie. `supplements` i `supplement_intakes` przechowują listę oraz log.
- `reminder_rules`, `reminder_occurrences` i `notification_outbox`
  zapewniają trwałość i deduplikację; `data_freshness` odróżnia czas odbioru
  od dnia obserwacji. `data_sync_ranges` potwierdza również cały odczytany
  zakres, nawet gdy nie było treningów. Ustawienia i historia tematów
  proaktywnych są w `proactive_alert_settings` i `proactive_alert_events`.
  Nieznany lub stary pomiar nigdy nie staje się zerem.

Ta pamięć aplikacji opisuje użytkownika i jego dane zdrowotne. Kontekst
agentów programistycznych utrzymujemy oddzielnie w dokumentach repozytorium.

## Pułapki potwierdzone w kodzie

- Health Connect nie daje tu stabilnego ID pozycji żywieniowej. Hash
  uwzględnia dzień, nazwę, zaokrąglone kcal/białko i numer wystąpienia.
  Zachowaj rozróżnienie identycznych produktów i odporność na szum float.
- Dzień żywienia wyznacza `end_time` całodobowego rekordu Fitatu.
  To konwencja źródła, nie rzeczywisty czas posiłku; `meal` nie jest
  podstawą wiarygodnego wykrywania obiadu/kolacji.
- Polling i `/sync` mają okno 4 dni, rozszerzane od ostatniego udanego syncu
  z zakładką, maksymalnie 60 dni. Wspólna blokada advisory PostgreSQL zapobiega
  równoległemu zapisowi schedulera i bota. Większa luka wymaga ręcznego ingestu.
- Scheduler żyje w API i startuje tylko przy `SCHEDULER_ENABLED=true`.
  Działa w strefie UTC; część logiki używa `date.today()`. Przy zmianach
  granic dnia sprawdzaj UTC/Europe/Warsaw.
- Development i production mają osobne bazy oraz boty/tokeny Telegrama.
  Wspólny token przy dwóch procesach long polling powoduje konflikt.
- CSP w `api/app.py` ma `script-src 'self'`: nowy kod frontendu nie może
  używać skryptów ani handlerów inline (`onclick=`), tylko plików z
  `api/static/`. Atrybuty `style` pozostają dozwolone (`unsafe-inline` dla
  CSS) dla pozycji tooltipów i szerokości wskaźników.
- Tryb dashboardu (`local`, `tailscale`, `public`, `disabled`) opisuje
  oczekiwany ingress, steruje odpowiedzią Telegrama i pozwala zablokować
  router, ale sam nie otwiera portów. Tailscale Serve lub Caddy pozostają
  konfiguracją hosta. Opcjonalna allowlista CIDR ufa `X-Forwarded-For` tylko
  od adresów wpisanych w `DASHBOARD_TRUSTED_PROXIES`; bez zaufanego proxy
  używa bezpośredniego adresu klienta.
- Alert braku treningu wymaga świeżego zakresu Intervals obejmującego pełne
  96 godzin i uwzględnia ręczne wpisy siłowe. Białko dotyczy zapisanych danych,
  a alert wagi tylko celu redukcji. To obserwacje, nie monitoring ani diagnoza.
- Wycofany alert `check_stale_sources` nie jest rejestrowany w schedulerze:
  mieszał brak nowych pomiarów z awarią synchronizacji, działał niezależnie
  od dashboardu i mógł wysyłać wiadomości po zmianie daty UTC. Wszystkie
  użytkowe alerty są teraz kontrolowane wyłącznie w sekcji dashboardu.
- Ewaluacja usuwa `agent_runs` i ingeruje w dane; wymaga `APP_ENV=test`, a
  `all` nie obejmuje coaches/import.
- `undo-import` nie przywraca profilu. Część przywracania nieaktywnych
  faktów opiera się na czasie aktualizacji, nie wyłącznie relacji do
  dokumentu — nie traktuj tego jako transakcyjnego rollbacku wszystkiego.
- Backup uruchamia `pg_dump` względem `DATABASE_URL`, a zdjęcia pakuje
  osobno z manifestem; obraz aplikacji zawiera
  kompatybilnego klienta PostgreSQL. Nie montuj socketa Dockera w kontenerze
  aplikacji.

## Co jest planem, a nie gotową funkcją

Repo zawiera główne ścieżki ingestii, specjalistów, Telegram, profil,
import wiedzy, polling, alerty, backupy oraz przenośne wdrożenie Compose.
`migrate` stosuje Alembic przed startem jednego workera API; scheduler jest
w nim warunkowy, a bot jest osobną usługą. Docker i warstwa HTTPS pozostają
usługami hosta.
Feedback reakcji Telegram, opcjonalne podsumowania, `/sync` Intervals.icu,
atomowe wpisy zbiorcze, historia samopoczucia, korelacje, dashboard,
lokalne zdjęcia, suplementy, trwałe przypomnienia i proaktywne alerty są gotowe w kodzie.
Brak jeszcze produkcyjnego ingestora Fitatu API i obsługi głosu.
Przed rozpoczęciem tych zadań sprawdź `TODO.md` i `PLAN.md`.
