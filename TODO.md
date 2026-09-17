# TODO / pomysły na rozwój

Stan na 2026-09-17. Fazy 0-3 z planu zakończone (MVP działa: ingestia, agenci,
Telegram, alerty, backup). Poniżej wszystko, co jeszcze nie jest zrobione -
z planu i z burzy mózgów. Kolejność w sekcjach = sugerowany priorytet.
Historia decyzji i znalezisk technicznych: `scripts/README.md`.

## Rekomendowane jako następne (duża wartość, mały koszt)

- [ ] **Cele i profil użytkownika** - tabela `goals` (kalorie, białko, waga
  docelowa, km/tydzień) + narzędzie `get_goals`; każdy specjalista porównuje
  z celem zamiast tylko raportować liczby. Warunek dla nudge'y (niżej) i
  fundament pod pełny profil (sekcja "Profil użytkownika").
- [ ] **Szacunek całodniowego wydatku kalorycznego** - żadne źródło tego nie
  daje (potwierdzone: Intervals.icu, Health Connect, Health Sync). Policzyć:
  BMR (Mifflin-St Jeor z wagi z Fitdays + wzrost/wiek z profilu) + kalorie z
  treningów (są) + składowa z kroków (są). Zawsze oznaczać jako szacunek.
  Zamyka najczęstsze pytanie ("jaki deficyt?") bez ręcznego "spaliłem X".
- [~] **Miara jakości w `scripts/eval_agents.py`** - sędzia LLM (Haiku, 5
  kryteriów kontraktu odpowiedzi) jest dla RunningCoach; do zrobienia dla
  pozostałych agentów przy ich "trenowaniu". Sędzia NIE weryfikuje
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
- [ ] **RecoveryAnalyst** - baseline HRV/RHR 7/28 dni z z-score, dług snu 7 dni,
  prosty readiness score; prompt: interpretuj względem bazy, nigdy wartości
  bezwzględne; rekomenduj dzień lekki/mocny. Dziś przy pytaniu o kilka dni
  woła get_recovery_day 3x - narzędzie `get_recovery_baseline(days)` załatwi
  to jednym wywołaniem.
- [ ] **NutritionCoach** - średnie 7-dniowe vs cel, białko g/kg z ostatniej wagi,
  rozkład dni (weekendy), 'co jadłem bogatego w X'; prompt: matematyka celów,
  timing wokół treningu (pyta running o godzinę), ograniczenia z profilu.
  Wymaga celów (`goals`).
- [ ] **BodyCompCoach** - średnia krocząca 7 dni, nachylenie kg/tydz., porównanie
  z oczekiwanym z deficytu (pyta nutrition); prompt: nie reaguj na pojedynczy
  odczyt (BIA = szum).
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
- [ ] **StrengthCoach z prawdziwym schematem** - dziś najsłabszy agent (luźny
  JSON + jedno narzędzie `get_recent_manual_logs`). Ustrukturyzowany zapis
  (ćwiczenie, serie, powtórzenia, ciężar) → progresja per ćwiczenie,
  szacowany 1RM, objętość per partia, rekordy.
- [ ] **Analiza korelacji → `agent_memory`** - mechanizm pamięci istnieje,
  prawie nieużywany. Cotygodniowy job: recovery/running szukają wzorców
  ("HRV spada po dwóch mocnych biegach", "gorszy sen po treningu po 20:00")
  i zapisują jako fakty. Potem odpowiedzi są o użytkowniku, nie ogólne.
- [ ] **Plan treningowy jako punkt odniesienia** - cel (np. półmaraton w
  dacie X) + tygodniowy plan (Intervals.icu ma API na zaplanowane treningi)
  → "jesteś 12 km za planem, ale HRV mówi, że słusznie odpuściłeś".

## Profil użytkownika (pomysł użytkownika, zweryfikowany - ma sens)

- [ ] **Tabela `user_profile`** (sekcje tekstowe: staż treningowy, kontuzje,
  problemy zdrowotne np. refluks, preferencje/alergie żywieniowe,
  doświadczenia, cele) wstrzykiwana do system promptu KAŻDEGO specjalisty -
  tak jak dziś fakty z `agent_memory`. Rozdzielić: profil = fakty podane
  przez użytkownika; `agent_memory` = wnioski wyciągnięte przez agentów.
- [ ] **Profilowanie w trakcie rozmowy** - narzędzie `update_profile(sekcja,
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
