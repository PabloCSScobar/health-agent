# Mapa architektury health-agent

Zweryfikowana przez odczyt kodu 2026-09-18; nie jest potwierdzeniem stanu
uruchomionych usług ani danych w bazie. Przy zmianach aktualizuj opis
odpowiedniej części, a plany utrzymuj w `TODO.md` i `PLAN.md`.

## Przepływ danych

1. Intervals.icu (treningi i wellness z Suunto) jest odpytywane przez
   `ingest/intervals.py`. `scheduler.py` i CLI korzystają z `ingest_range`.
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

- `settings.py`: ustawienia środowiska i `.env`; `settings` powstaje przy imporcie.
- `db/models.py`: tabele i ograniczenia; `db/session.py`: sesja z commit/rollback.
  Zmiany schematu mają migracje w katalogu repo `alembic/versions/`.
- `api/app.py`: FastAPI, autoryzacja webhooka, start/stop schedulera.
- `scheduler.py`: polling, stan synchronizacji, alerty na Telegram i backup.
- `ingest/`: normalizacja źródeł i dedup, bez generowania odpowiedzi LLM.
- `tools/`: narzędzia domenowe oraz zapis wpisów ręcznych, profilu i wiedzy.
- `agents/base.py`: fabryka modeli, prompt caching, async run, rejestracja
  kosztu/czasu/narzędzi i drzewa wywołań w `agent_runs` przez `ContextVar`.
- `agents/registry.py`: routing, narzędzia, onboarding, wspólny kontrakt
  specjalistów i dołączanie profilu/digestu wiedzy.
- `prompts/{running,strength,nutrition,body,recovery}.md`: metodologia domenowa.
- `agents/importer.py`: ekstrakcja, rekoncyliacja, raport i cofnięcie importu.
- `channels/telegram.py`: autoryzacja czatu, tekst/dokumenty, historia i komendy.
- `cli/main.py`: parser komend i wywołanie istniejących funkcji aplikacji.
- `config/agents.yaml` w repo: wybór modeli per agent, wczytywany przez
  `agents/base.py` przy imporcie; zmiana wymaga restartu procesu.

## Agenci aplikacji

Orchestrator kieruje do `running`, `strength`, `nutrition`, `body`,
`recovery`. Delegacja i wpis ręczny są output functions: wynik funkcji
jest finalną odpowiedzią bez dodatkowej parafrazy routera. Zapis wielu
faktów profilowych jest zwykłym narzędziem, żeby nie gubić kolejnych zapisów.

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
- `manual_logs`: ręczne wpisy i oryginalny tekst; obsługa w `tools/manual.py`.
- `agent_memory`: profil pod `user_profile` i dedup alertów pod
  `system_alerts`. Komentarze sugerujące wyłącznie profil są niepełne.
- `documents`: oryginały importowanych notatek, data, skrót i hash dedup.
- `knowledge`: fakty/wnioski z domeną, pochodzeniem, datą, pewnością,
  aktywnością i `superseded_by`; digest plus narzędzia odczytu szczegółów.
- `conversations`: historia czatu. `agent_runs`: model, tokeny, koszt,
  czas, nazwy wywołanych narzędzi, rodzic wywołania.

Ta pamięć aplikacji opisuje użytkownika i jego dane zdrowotne. Kontekst
agentów programistycznych utrzymujemy oddzielnie w dokumentach repozytorium.

## Pułapki potwierdzone w kodzie

- Health Connect nie daje tu stabilnego ID pozycji żywieniowej. Hash
  uwzględnia dzień, nazwę, zaokrąglone kcal/białko i numer wystąpienia.
  Zachowaj rozróżnienie identycznych produktów i odporność na szum float.
- Dzień żywienia wyznacza `end_time` całodobowego rekordu Fitatu.
  To konwencja źródła, nie rzeczywisty czas posiłku; `meal` nie jest
  podstawą wiarygodnego wykrywania obiadu/kolacji.
- Polling ma okno 3 dni, rozszerzane od ostatniego udanego syncu z zakładką,
  maksymalnie 60 dni. Większa luka wymaga ręcznego ingestu.
- Scheduler żyje w API, w strefie UTC; część logiki używa `date.today()`.
  Przy zmianach granic dnia sprawdzaj UTC/Europe/Warsaw.
- Alerty oceniają daty ostatniego treningu/wagi/żywienia, nie pełny stan
  wszystkich integracji. Nie są niezależnym monitoringiem awarii API.
- Ewaluacja usuwa `agent_runs` i ingeruje w dane; `all` nie obejmuje
  coaches/import. Opis bezpieczeństwa na początku skryptu jest mylący.
- `undo-import` nie przywraca profilu. Część przywracania nieaktywnych
  faktów opiera się na czasie aktualizacji, nie wyłącznie relacji do
  dokumentu — nie traktuj tego jako transakcyjnego rollbacku wszystkiego.
- Backup uruchamia `pg_dump` względem `DATABASE_URL`; obraz aplikacji zawiera
  kompatybilnego klienta PostgreSQL. Nie montuj socketa Dockera w kontenerze
  aplikacji.

## Co jest planem, a nie gotową funkcją

Repo zawiera główne ścieżki ingestii, specjalistów, Telegram, profil,
import wiedzy, polling, alerty, backupy oraz przenośne wdrożenie Compose.
`migrate` stosuje Alembic przed startem jednego workera API ze schedulerem;
bot jest osobną usługą. Docker/Tailscale pozostają usługami hosta.
Brak jeszcze feedbacku reakcji Telegram, produkcyjnego ingestora Fitatu API,
`/sync`, cotygodniowych podsumowań, zadań korelacyjnych, dashboardu, obsługi
zdjęć i głosu.
Przed rozpoczęciem tych zadań sprawdź `TODO.md` i `PLAN.md`.
