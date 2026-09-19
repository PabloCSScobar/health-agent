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
- `scheduler.py`: polling, alerty na Telegram i backup; transakcyjny catch-up
  Intervals.icu oraz blokada między procesami są w `ingest/sync.py`.
- `ingest/`: normalizacja źródeł i dedup, bez generowania odpowiedzi LLM.
- `tools/`: narzędzia domenowe oraz zapis wpisów ręcznych, profilu i wiedzy.
- `agents/base.py`: fabryka modeli, prompt caching, async run, rejestracja
  kosztu/czasu/narzędzi i drzewa wywołań w `agent_runs` przez `ContextVar`.
- `agents/registry.py`: routing, narzędzia, onboarding, wspólny kontrakt
  specjalistów i dołączanie profilu/digestu wiedzy.
- `prompts/{running,strength,nutrition,body,recovery}.md`: metodologia domenowa.
- `agents/importer.py`: ekstrakcja, rekoncyliacja, raport i cofnięcie importu.
- `channels/telegram.py`: autoryzacja czatu, tekst/dokumenty, historia, komendy i reakcje feedbacku.
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
- `agent_memory`: profil pod `user_profile` i dedup alertów pod
  `system_alerts` i podsumowań pod `system_summaries`. Komentarze sugerujące wyłącznie profil są niepełne.
- `documents`: oryginały importowanych notatek, data, skrót i hash dedup.
- `knowledge`: fakty/wnioski z domeną, pochodzeniem, datą, pewnością,
  aktywnością i `superseded_by`; digest plus narzędzia odczytu szczegółów.
- `conversations`: historia czatu oraz Telegram `message_id` (wszystkie części) i root run odpowiedzi.
  `feedback`: jedna aktualna ocena 👍/👎 z opcjonalnym komentarzem na odpowiedź.
  `agent_runs`: model, tokeny, koszt, czas, nazwy narzędzi i rodzic wywołania;
  raport wieloagentowy ma syntetyczny korzeń `parallel-specialists`.

Ta pamięć aplikacji opisuje użytkownika i jego dane zdrowotne. Kontekst
agentów programistycznych utrzymujemy oddzielnie w dokumentach repozytorium.

## Pułapki potwierdzone w kodzie

- Health Connect nie daje tu stabilnego ID pozycji żywieniowej. Hash
  uwzględnia dzień, nazwę, zaokrąglone kcal/białko i numer wystąpienia.
  Zachowaj rozróżnienie identycznych produktów i odporność na szum float.
- Dzień żywienia wyznacza `end_time` całodobowego rekordu Fitatu.
  To konwencja źródła, nie rzeczywisty czas posiłku; `meal` nie jest
  podstawą wiarygodnego wykrywania obiadu/kolacji.
- Polling i `/sync` mają okno 3 dni, rozszerzane od ostatniego udanego syncu
  z zakładką, maksymalnie 60 dni. Wspólna blokada advisory PostgreSQL zapobiega
  równoległemu zapisowi schedulera i bota. Większa luka wymaga ręcznego ingestu.
- Scheduler żyje w API i startuje tylko przy `SCHEDULER_ENABLED=true`.
  Działa w strefie UTC; część logiki używa `date.today()`. Przy zmianach
  granic dnia sprawdzaj UTC/Europe/Warsaw.
- Development i production mają osobne bazy oraz boty/tokeny Telegrama.
  Wspólny token przy dwóch procesach long polling powoduje konflikt.
- Alerty oceniają daty ostatniego treningu/wagi/żywienia, nie pełny stan
  wszystkich integracji. Nie są niezależnym monitoringiem awarii API.
- Ewaluacja usuwa `agent_runs` i ingeruje w dane; wymaga `APP_ENV=test`, a
  `all` nie obejmuje coaches/import.
- `undo-import` nie przywraca profilu. Część przywracania nieaktywnych
  faktów opiera się na czasie aktualizacji, nie wyłącznie relacji do
  dokumentu — nie traktuj tego jako transakcyjnego rollbacku wszystkiego.
- Backup uruchamia `pg_dump` względem `DATABASE_URL`; obraz aplikacji zawiera
  kompatybilnego klienta PostgreSQL. Nie montuj socketa Dockera w kontenerze
  aplikacji.

## Co jest planem, a nie gotową funkcją

Repo zawiera główne ścieżki ingestii, specjalistów, Telegram, profil,
import wiedzy, polling, alerty, backupy oraz przenośne wdrożenie Compose.
`migrate` stosuje Alembic przed startem jednego workera API; scheduler jest
w nim warunkowy, a bot jest osobną usługą. Docker i warstwa HTTPS pozostają
usługami hosta.
Feedback reakcji Telegram, opcjonalne podsumowania dzienne i tygodniowe,
`/sync` Intervals.icu, atomowe wpisy zbiorcze oraz historia samopoczucia są
gotowe. Brak jeszcze produkcyjnego ingestora Fitatu API, zadań korelacyjnych,
dashboardu, obsługi zdjęć i głosu.
Przed rozpoczęciem tych zadań sprawdź `TODO.md` i `PLAN.md`.
