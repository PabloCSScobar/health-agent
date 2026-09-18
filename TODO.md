# TODO / pomysły na rozwój

Stan na 2026-09-18. Plan implementacji otwartych punktów: `PLAN.md`. Fazy 0-3 z planu zakończone (MVP działa: ingestia, agenci,
Telegram, alerty, backup). Poniżej wszystko, co jeszcze nie jest zrobione -
z planu i z burzy mózgów. Kolejność w sekcjach = sugerowany priorytet.
Historia decyzji i znalezisk technicznych: `scripts/README.md`.

## Rekomendowane jako następne (duża wartość, mały koszt)

- [x] **Cele i profil użytkownika** - `tools/profile.py` (agent_memory pod
  `user_profile`, wstrzykiwany do każdego specjalisty, `set_user_profile_facts`
  w orchestratorze). Użytkownik musi PODAĆ wartości na Telegramie ("mam 34
  lata, 182 cm, cel 84 kg") - dziś profil jest pusty.
- [x] **Szacunek całodniowego wydatku** - `tools/energy.py` + `get_energy_balance`
  w nutrition. Wymaga wzrostu/wieku/płci w profilu.
- [x] **Miara jakości w `scripts/eval_agents.py`** - sędzia LLM (Haiku, 5
  kryteriów kontraktu) dla wszystkich pięciu specjalistów. Sędzia NIE weryfikuje
  poprawności liczb (nie widzi wyników narzędzi) - gdyby to było potrzebne:
  zapisywać wyniki narzędzi w `agent_runs` i dawać je sędziemu.
- [ ] **`/sync` na Telegramie + narzędzie "odśwież dzisiejsze jedzenie"** -
  `ingest_range` i klient Fitatu (`scripts/test_fitatu.py`) już istnieją,
  tylko nie są podpięte. Uwaga: token Fitatu z `.env` wygasa po godzinie
  (JWT `exp`), `FITATU_REFRESH_TOKEN` jest pusty - najpierw refresh flow.
- [ ] **Tygodniowe podsumowanie** (niedziela wieczór: km, trend wagi, średni
  deficyt, sen, HRV, co poszło dobrze/źle). Dzienne raporty świadomie
  odrzucone jako szum - tygodniowe to inna kategoria. Wszystkie klocki są.

## Faza 5b - jakość/specjalizacja agentów (zgłoszone przez użytkownika)

Metoda (uzgodniona 2026-09-17, pilot = RunningCoach, ZROBIONY): 4 dźwignie w
kolejności wpływu - (1) narzędzia liczące agregaty/z-score/trendy w Pythonie,
(2) metodologia domenowa w `prompts/<agent>.md`, (3) kontrakt odpowiedzi
(liczby -> interpretacja względem bazy -> JEDNA rekomendacja -> pewność) +
przykłady z fikcyjnymi liczbami, (4) sędzia LLM w `eval_agents.py`. Szczegóły
i wnioski: `scripts/README.md` (wpis 2026-09-17). Ten sam wzorzec powtórzyć
dla reszty, jeden agent naraz:
- [x] **RunningCoach** - `tools/running.py` (7 narzędzi), `prompts/running.md`,
  `eval_agents.py running` (7 testów + sędzia).
- [x] **RecoveryAnalyst** - `get_recovery_baseline`, `prompts/recovery.md`.
- [x] **NutritionCoach** - `get_nutrition_summary`/`get_energy_balance`/`find_foods`,
  `prompts/nutrition.md`.
- [x] **BodyCompCoach** - `get_body_trend`, `prompts/body.md`.
- [ ] Ograniczenie pydantic-ai: dwie output functions w jednej turze -> tylko
  pierwsza się wykonuje (np. 'waga 87 i spaliłem 2600' zapisze jedno).
  Jeśli będzie przeszkadzać: `log_manual_entries(list)` jak przy profilu.
- [ ] **Pętla zwrotna z produkcji**: 👍/👎 pod odpowiedzią na Telegramie
  (MessageReactionHandler w python-telegram-bot) zapisane przy `agent_runs`
  (wymaga `message_id` w `conversations` - migracja). Bez tego każda zmiana
  promptu opiera się na 6 syntetycznych pytaniach, nie na realnym użyciu.
- [ ] **Running na Haiku 4.5** - 3x `eval_agents.py running` (jest już sędzia,
  więc da się to uczciwie ocenić); jeśli 21/21 - kolejne ~40% taniej na
  najdroższym agencie.
- [ ] Latencja głębokiej analizy biegu to 40-60 s (4-5 rund narzędzi + recovery).
  Akceptowalne dla 'analizy', za długie dla 'szybkiego pytania' - rozważyć
  streaming statusu na Telegram ('sprawdzam regenerację...') zamiast samego
  'pisze...'.
- [x] **StrengthCoach** - `tools/strength.py` (schemat, parser, e1RM, objętość,
  PR, grupy mięśniowe), `prompts/strength.md`. Do przetestowania dopiero po
  kilku prawdziwych wpisach.
- [ ] **Analiza korelacji → `agent_memory`** - mechanizm pamięci istnieje,
  prawie nieużywany. Cotygodniowy job: recovery/running szukają wzorców
  ("HRV spada po dwóch mocnych biegach", "gorszy sen po treningu po 20:00")
  i zapisują jako fakty. Potem odpowiedzi są o użytkowniku, nie ogólne.
- [ ] **Plan treningowy jako punkt odniesienia** - cel (np. półmaraton w
  dacie X) + tygodniowy plan (Intervals.icu ma API na zaplanowane treningi)
  → "jesteś 12 km za planem, ale HRV mówi, że słusznie odpuściłeś".

## Profil użytkownika - ZROBIONE 2026-09-17 (tools/profile.py, /profil, wywiad, dopytywanie ❓)

- [x] **Tabela `user_profile`** (zrealizowane jako `agent_memory` pod `user_profile`, nie osobna tabela) (sekcje tekstowe: staż treningowy, kontuzje,
  problemy zdrowotne np. refluks, preferencje/alergie żywieniowe,
  doświadczenia, cele) wstrzykiwana do system promptu KAŻDEGO specjalisty -
  tak jak dziś fakty z `agent_memory`. Rozdzielić: profil = fakty podane
  przez użytkownika; `agent_memory` = wnioski wyciągnięte przez agentów.
- [x] **Profilowanie w trakcie rozmowy** - narzędzie `update_profile(sekcja,
  tekst)` dla orchestratora: gdy użytkownik mówi "mam refluks" / "biegam od
  3 lat", zapis bez osobnego trybu. Plus opcjonalny `/profil` (podgląd +
  edycja) i krótki onboarding ("zadam Ci kilka pytań") na start.
- Uwagi techniczne: profil trzymać kompaktowo (kilkaset tokenów) - idzie do
  każdego wywołania; umieścić w system prompcie, bo jest statyczny → trafia
  w prompt cache za darmo. Przy problemach zdrowotnych agent ma podpowiadać
  ostrożnie i jasno mówić, że to nie porada medyczna.

## Suplementy i przypomnienia (pomysł użytkownika, zweryfikowany - częściowo)

- [ ] **Tabela `supplements`** (nazwa, dawka, harmonogram: godzina ALBO
  posiłek, aktywny od/do) + log "wzięte/pominięte" (potwierdzenie z
  Telegrama jednym słowem, snooze). NutritionCoach widzi suplementację.
- [ ] **Przypomnienia o stałej godzinie** - proste: cron w istniejącym
  schedulerze + `_send_telegram_message` (jak alerty). Bez ryzyka.
- [ ] **Przypomnienia "do obiadu/kolacji"** - UWAGA, ograniczenie: Health
  Connect przez Fitatu NIE przekazuje typu posiłku ani godziny - każda
  pozycja ma `start_time`/`end_time` = cała doba, `meal = None` (sprawdzone
  na prawdziwych danych, komentarz w `ingest/healthconnect.py`). "Wykrycie
  obiadu" wprost z tego kanału jest niemożliwe. Realne opcje:
  1. **Heurystyka okna czasowego** (rekomendowana): nowe `nutrition_items`
     na dziś pojawiły się w syncu w oknie obiadowym (np. 12-15) → uznaj, że
     był obiad → przypomnij. Opóźnienie = interwał syncu apki Health Connect
     Webhook (ustawić np. co 15-30 min). Działa z obecnym pipeline'em.
  2. Polling bezpośredniego API Fitatu (tam typ posiłku JEST) - wymaga
     działającego refresh tokenu (dziś brak) i częstego odpytywania
     nieoficjalnego API (ryzyko regulaminowe opisane w `scripts/README.md`).
  3. Rezygnacja z detekcji: przypomnienie o typowej porze posiłku z pytaniem
     "brałeś X do obiadu?" - najprostsze, zero zależności.

## Zdjęcia sylwetki (pomysł użytkownika) - TYLKO archiwum i porównanie, BEZ analizy przez agenta

Decyzja użytkownika (2026-09-16): zdjęcia NIE są analizowane przez model.
Zdjęcia nigdy nie opuszczają lokalnej maszyny (nie lecą do API Anthropic).

- [ ] **Odbiór zdjęć z Telegrama** → plik na dysku (`data/photos/`,
  gitignored - NIE blob w Postgresie) + wiersz w `progress_photos` (data,
  typ ujęcia przód/bok/tył, notatka, waga i % tłuszczu z `body_composition`
  z tego dnia - żeby porównanie w czasie było liczba+obraz, nie sam obraz).
  Typ ujęcia i notatka z podpisu zdjęcia na Telegramie (np. "przód").
- [ ] **Porównanie w czasie** - widok w dashboardzie (patrz "Niezawodność"):
  zdjęcia tego samego typu obok siebie, chronologicznie, z wagą/% tłuszczu
  pod każdym. Ewentualnie na Telegramie: `/foto przód` → ostatnie N zdjęć
  tego typu jako album.
- [ ] Zachęcać do standaryzacji (ta sama poza, rano po ważeniu, to samo
  miejsce/światło) - inaczej porównanie w czasie nic nie mówi.
- [ ] **Backup**: `pg_dump` NIE obejmuje plików - `data/photos/` trzeba
  dołożyć do strategii backupu (patrz "Niezawodność").

## Wygoda wprowadzania danych

- [ ] **Wiadomości głosowe na Telegramie** - transkrypcja (Whisper) → ten sam
  pipeline. Największy zysk na siłowni ("ławka 4 na 8 po 80" między
  seriami).
- [ ] **Samopoczucie rano (1-5) + notatki** - `kind="wellbeing"`/`"note"`
  istnieją w `ManualLog`, ale nic ich nie czyta. Skorelować ze snem/HRV.
  Także kontuzje/leki - recovery agent powinien o nich wiedzieć.

## Niezawodność i dostęp

- [x] Backfill luki w Intervals.icu po dłuższym przestoju - `ingest_state`
  + okno liczone od ostatniego syncu (2026-09-18, scripts/README.md).
- [ ] **Suunto → Intervals.icu nie zawsze synchronizuje się automatycznie.**
  2026-09-18: HRV/tętno spoczynkowe/sen dla 17-18.09 brakowało - potwierdzone,
  że to NIE nasz błąd (Intervals.icu zwracało `hrv: null` już we własnym
  API, nasz polling działał poprawnie). Dane pojawiły się dopiero po (a)
  ręcznym otwarciu apki Suunto na telefonie (wymusiło sync zegarek->telefon
  ->Suunto Cloud) i (b) ręcznym "pobierz stare dane" w Intervals.icu. Czyli
  ogniwo Suunto Cloud -> Intervals.icu w tej konfiguracji NIE jest w pełni
  automatyczne/push - albo jest, ale zawodzi bez wyraźnej przyczyny; nie da
  się tego zdiagnozować ani naprawić z naszej strony (poza naszym systemem).
  Do zrobienia (nie teraz, odłożone przez użytkownika):
  1. Sprawdzić w ustawieniach konta Intervals.icu, czy jest opcja
     częstotliwości/trybu synchronizacji z Suunto.
  2. Sprawdzić ustawienia telefonu (oszczędzanie baterii może ubijać apkę
     Suunto w tle, więc watch->phone też nie synchronizuje się samo).
  3. **Poszukać alternatywy dla Intervals.icu jako źródła wellness
     (HRV/sen/RHR)** jeśli problem się powtórzy - np. bezpośrednie API
     Suunto (apizone.suunto.com, patrz Faza 4 w planie - odrzucone
     wcześniej głównie dlatego, że Intervals.icu "po prostu działało";
     ten incydent podważa to założenie), Health Sync (już sprawdzone
     wcześniej, że nie eksportuje HRV/snu do Health Connect - do
     re-weryfikacji, mogło się zmienić), albo inna platforma z integracją
     Suunto (Garmin Connect nie dotyczy, to inny ekosystem).
  4. Do czasu decyzji: alert o martwym źródle (już działa, próg 48h) jest
     jedyną siatką bezpieczeństwa - żadna dodatkowa praca nie jest pilna,
     dopóki się nie powtórzy.
- [ ] **PILNE: autostart procesów po restarcie WSL.** 2026-09-17: po restarcie
  WSL/Dockera baza wstała sama (`restart: unless-stopped`), ale uvicorn
  (webhook + scheduler: polling, alerty, backup) i bot Telegram to procesy
  odpalane ręcznie przez `nohup` - zniknęły, webhooki z telefonu nie
  działały, aż użytkownik zauważył. Alert o martwym źródle też nie przyszedł,
  bo żyje w tym samym procesie. Opcje: (a) oba jako usługi w
  `docker-compose.yml` z `restart: unless-stopped` (najspójniej z Fazą 6),
  (b) systemd --user + `loginctl enable-linger`, (c) Task Scheduler w
  Windows odpalający `wsl -d ... -- uv run ...` przy logowaniu.
- [ ] **Przenosiny na VPS/RPi (Faza 6) - ważniejsze niż się wydaje.**
  Webhook, polling, alerty i backup żyją w JEDNYM procesie na PC
  (`uvicorn`). Health Connect Webhook ma lookback 48h → PC wyłączony na
  weekend = dane bezpowrotnie stracone, a alert o tym też nie przyjdzie (ten
  sam proces). Docker Compose i Tailscale już gotowe pod przenosiny.
- [ ] **Backup poza dyskiem PC** - `backups/` leży na tym samym dysku co
  baza. Sync do chmury / drugiego node'a Tailscale / innego urządzenia.
- [ ] **Deterministyczne nudge'e** (nie LLM, reguły w schedulerze jak
  alerty): "4 dni bez treningu", "białko poniżej celu 3 dni z rzędu", "waga
  rośnie 2 tygodnie". Zero kosztu. Wymaga celów (pierwsza sekcja).
- [ ] **Prosty dashboard** - FastAPI już jest; jedna strona z wykresami
  (waga, HRV, km/tydzień, deficyt, zdjęcia obok siebie). Lokalnie przez
  Tailscale, bez wystawiania danych na zewnątrz.
- [ ] Alert o wygasłym tokenie Fitatu - niski priorytet, dopóki Fitatu jest
  tylko ścieżką zapasową.

## Sugerowana kolejność dla pomysłów użytkownika (uzgodnione 2026-09-16)

1. Profil użytkownika (sekcja "Profil użytkownika") → 2. cele (`goals`) →
3. suplementy: najpierw stała godzina, potem heurystyka okna posiłku →
4. zdjęcia sylwetki (archiwum + porównanie, bez analizy).

## Świadomie odłożone

- Analiza zdjęć sylwetki przez model - odrzucona (2026-09-16): zdjęcia mają
  być tylko archiwizowane i porównywane wizualnie, nie interpretowane przez
  LLM (prywatność + niska wiarygodność takiej oceny).

- Poranny/wieczorny raport dzienny - odrzucone jako szum (2026-09-16).
- Faza 4 (bezpośrednie Suunto Cloud API) - tylko jeśli potrzebne Training
  Effect/VO2max; zatwierdzenie dla osób prywatnych niepewne.

## Import wiedzy z zewnątrz - ZROBIONE 2026-09-18 (szczegóły: scripts/README.md)

Pierwsza propozycja (surowy tekst + ekstrakcja do profilu/agent_memory +
search_notes + potwierdzenie) po analizie: szkielet OK, ale 4 wady:
1. `agent_memory` (klucz-wartość, unikalny (agent,key)) nie nadaje się na
   dziesiątki faktów: nazwy kluczy z LLM niespójne, brak daty/źródła,
   kolejny import nadpisuje lub dubluje, każdy fakt na zawsze w każdym
   prompcie (puchnie).
2. Brak rekoncyliacji: nowy fakt sprzeczny ze starym ("cel 85" z czerwca vs
   "84" z dziś) - nie wiadomo, który wygrywa.
3. Wyszukiwanie po słowach kluczowych w polskiej fleksji zawodzi; embeddingi
   to zależność (pgvector) nieproporcjonalna do kilkudziesięciu krótkich
   notatek.
4. Fakty z cudzego streszczenia (Claude spekulował) trafiają do promptów bez
   przeglądu - wpływają na porady.

Docelowo:
- [x] Tabela `documents` (tytuł, źródło, data dokumentu, tekst, streszczenie
  3 linie, tagi/domeny) - oryginał w całości. NIE `manual_logs`.
- [x] Tabela `knowledge` (jedna dla wniosków agentów, importu i faktów z
  czatu): domena, rodzaj (fakt/zdarzenie/PB/preferencja/lekcja/decyzja),
  treść, data ważności (od/do lub data zdarzenia), źródło (document_id /
  agent / chat), pewność, aktywny. `remember_fact` docelowo pisze tutaj.
- [x] Import: plik .txt/.md na Telegramie (Document handler) albo CLI
  `health-agent import plik.md --domena running`; długi wklejony tekst
  (>~800 zn.) -> jedno pytanie "zaimportować jako notatkę? tak/nie".
- [x] Agent-importer (Sonnet, 1 wywołanie/dokument) dostaje ISTNIEJĄCĄ
  wiedzę z domen dokumentu i zwraca operacje: dodaj / zaktualizuj(id) /
  oznacz nieaktualne(id) / konflikt(id, pytanie) - rekoncyliacja, nie zrzut.
  Fakty profilowe -> profil z regułą "nowsza data wygrywa, remis = pytanie".
- [x] Potwierdzenie: numerowana lista ("zapisałem 14: 9 running, 3
  nutrition, 2 profil; 1 konflikt: cel wagi 85 vs 84 - który?"), "usuń 3, 7"
  jednym zdaniem. Aktywacja od razu, z łatwym cofnięciem (mniejsze tarcie
  niż kolejka "do zatwierdzenia").
- [x] Do promptu specjalisty tylko DIGEST domeny (aktywne, najważniejsze,
  limit ~1500 zn., z datami - żeby sam ocenił staleness); reszta przez
  narzędzie `get_knowledge(domena, rodzaj?, fraza?)` + `read_document(id)`
  (dokumenty są krótkie - czytać w całości, cache to tania operacja).
- [x] Eval: fixture-dokument -> liczba/domeny faktów, test konfliktu, test
  że digest nie przekracza limitu, test "PB z 2024 oznaczony datą".
- Świadomie pomijane: embeddingi/pgvector (do rewizji, gdy notatek >100).
- [ ] Otwarte: `undo-import` nie cofa zmian w profilu (profil nie ma historii)
  - jeśli będzie przeszkadzać, dodać historię wartości profilu.
- [ ] Otwarte: ekstrakcja ma wariancję (12-15 faktów z tego samego
  dokumentu, czasem pomija spekulacje zamiast oznaczać low) - akceptowalne,
  raport pokazuje wszystko; przy problemach: temperature=0 dla importera.
