# TODO / pomysły na rozwój

Stan na 2026-09-19. Plan implementacji otwartych punktów: `PLAN.md`. Fazy 0-3 z planu zakończone (MVP działa: ingestia, agenci,
Telegram, alerty, backup). Poniżej wszystko, co jeszcze nie jest zrobione -
z planu i z burzy mózgów. Kolejność w sekcjach = sugerowany priorytet.
Historia decyzji i znalezisk technicznych: `scripts/README.md`.

## Rekomendowane jako następne (duża wartość, mały koszt)

- [x] **Cele i profil użytkownika** - `tools/profile.py` (agent_memory pod
  `user_profile`, wstrzykiwany do każdego specjalisty, `set_user_profile_facts`
  w orchestratorze). Użytkownik musi PODAĆ wartości na Telegramie ("mam 34
  lata, 182 cm, cel 84 kg") - aplikacja nie uzupełnia ich automatycznie.
- [x] **Szacunek całodniowego wydatku** - `tools/energy.py` + `get_energy_balance`
  w nutrition. Wymaga wzrostu/wieku/płci w profilu.
- [x] **Miara jakości w `scripts/eval_agents.py`** - sędzia LLM (Haiku, 5
  kryteriów kontraktu) dla wszystkich pięciu specjalistów. Sędzia NIE weryfikuje
  poprawności liczb (nie widzi wyników narzędzi) - gdyby to było potrzebne:
  zapisywać wyniki narzędzi w `agent_runs` i dawać je sędziemu.
- [x] **`/sync` Intervals.icu na Telegramie** - wspólny z schedulerem catch-up
  (3 dni, zakładka od markera, limit 60 dni), blokada transakcyjna PostgreSQL
  przeciw równoległym procesom i atomowy zapis audytu, danych oraz markera.
- [ ] **Narzędzie "odśwież dzisiejsze jedzenie" z Fitatu** - klient
  (`scripts/test_fitatu.py`) istnieje, ale token z `.env` wygasa po godzinie
  (JWT `exp`), a `FITATU_REFRESH_TOKEN` jest pusty - najpierw refresh flow.
- [x] **Podsumowania dzienne i tygodniowe** - czterech specjalistów równolegle, komendy `/daily` i `/weekly`, CLI oraz opcjonalne joby cron w strefie Europe/Warsaw. Domyślnie wyłączone, żeby wdrożenie nie zaczęło wysyłać raportów bez decyzji użytkownika.

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
- [x] Ograniczenie pydantic-ai przy wielu wpisach rozwiązane przez jedną output
  function `log_manual_entries(list)`: cała lista jest walidowana i zapisywana
  atomowo, z payload-aware dedupem i zgodnością starego `log_manual_entry`.
- [x] **Pętla zwrotna z produkcji**: 👍/👎 pod odpowiedzią na Telegramie, komentarz w reply, powiązanie `conversations` z korzeniem `agent_runs` oraz CLI `health-agent feedback`.
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
- [x] **Analiza korelacji → `knowledge`** - deterministyczny dzienny frame,
  pięć jawnych par Spearmana, minimum 20 pełnych par i próg `|rho|>=0.4`.
  Wyniki są wersjonowane, a aktywna lekcja ma niską pewność i jawne
  zastrzeżenie o braku przyczynowości. CLI działa; job niedzielny jest
  domyślnie wyłączony do ręcznego przeglądu pierwszego wyniku.
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

- [x] **Tabela `supplements` + log** - CRUD w dashboardzie, `/suple`,
  potwierdzenia wzięte/pominięte i narzędzie NutritionCoach.
- [x] **Przypomnienia o stałej godzinie i warunkowe** - szkic wymaga
  potwierdzenia; warunki v1: kroki/białko poniżej progu oraz brak
  biegu/treningu w oknie. Reguły, wystąpienia i outbox są trwałe; nieświeże
  dane są ponawiane do 2 h, potem oznaczane jako pominięte.
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

- [x] **Odbiór zdjęć z Telegrama** → plik na dysku (`data/photos/`,
  gitignored - NIE blob w Postgresie) + wiersz w `progress_photos` (data,
  typ ujęcia przód/bok/tył, notatka, waga i % tłuszczu z `body_composition`
  z tego dnia - żeby porównanie w czasie było liczba+obraz, nie sam obraz).
  Typ ujęcia i notatka z podpisu zdjęcia na Telegramie (np. "przód").
- [x] **Porównanie w czasie** - widok w dashboardzie (patrz "Niezawodność"):
  zdjęcia tego samego typu obok siebie, chronologicznie, z wagą/% tłuszczu
  pod każdym. Ewentualnie na Telegramie: `/foto przód` → ostatnie N zdjęć
  tego typu jako album.
- [ ] Zachęcać do standaryzacji (ta sama poza, rano po ważeniu, to samo
  miejsce/światło) - inaczej porównanie w czasie nic nie mówi.
- [x] **Backup lokalny zdjęć**: osobne archiwum tar.gz z manifestem SHA-256
  powstaje obok dumpa bazy. Kopia poza VPS pozostaje osobnym, niższym
  priorytetem.

## Wygoda wprowadzania danych

- [ ] **Wiadomości głosowe na Telegramie** - transkrypcja (Whisper) → ten sam
  pipeline. Największy zysk na siłowni ("ławka 4 na 8 po 80" między
  seriami).
- [x] **Samopoczucie rano (1-5) + notatki** - wpisy `wellbeing` są ściśle
  walidowane w skali 1-5, a RecoveryAnalyst czyta historię ocen i notatek.
- [x] Skorelować samopoczucie ze snem/HRV.
- [ ] Kontuzje i leki dodać do jawnego kontraktu profilu/wiedzy, zamiast
  wyciągać je z dowolnych notatek.

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
- [x] **Autostart aplikacji i przenośne wdrożenie.** Dockerfile + Compose
  uruchamiają bazę, migracje, pojedyncze API ze schedulerem i bota; procesy
  mają `restart: unless-stopped`. Są skrypty instalacji, diagnostyki i
  aktualizacji oraz instrukcja WSL/VPS. Backup nie zależy już od socketa
  Dockera. Restart procesów API i bota został sprawdzony wraz z healthcheckiem;
  pełny restart VPS-a i test webhooka z telefonu również przeszły.
- [x] **Przenosiny na VPS (Faza 6).** Host z Dockerem, reverse proxy HTTPS,
  webhook i autostart są skonfigurowane; lokalny bot i scheduler production
  zostały wyłączone. Szczegóły konkretnego hosta pozostają w ignorowanym
  `VPS_LOCAL.md`, bez sekretów w repozytorium.
- [x] **Rozdzielenie development/production.** `APP_ENV` identyfikuje
  środowisko, `SCHEDULER_ENABLED` wyłącza lokalne zadania okresowe, a bot
  pokazuje oba ustawienia w `/status`. Równoległy dev wymaga osobnego bota
  i tokenu Telegrama.
- [ ] **Backup poza VPS** - wolumen backupów leży na tym samym hoście co baza.
  Sync do chmury / drugiego node'a Tailscale / innego urządzenia.
- [ ] **Deterministyczne nudge'e** (nie LLM, reguły w schedulerze jak
  alerty): "4 dni bez treningu", "białko poniżej celu 3 dni z rzędu", "waga
  rośnie 2 tygodnie". Zero kosztu. Wymaga celów (pierwsza sekcja).
- [x] **Dashboard** - `/dash` za publicznym HTTPS i logowaniem Argon2id;
  zakresy 7/30/90 dni, korelacje, zdjęcia, suplementy i przypomnienia.
  Sesje są w PostgreSQL, mutacje mają CSRF i kontrolę Origin.
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
