# Skrypty testowe (Faza 0 / smoke testy)

Cel: sprawdzić każdą zewnętrzną zależność z osobna, zanim zacznie się budować
architekturę core (baza, orkiestracja agentów, ingestory). Pełny plan:
`~/.claude/plans/chc-skonfigurowa-agenta-aplikacj-kt-ra-zazzy-wilkinson.md`.

Uruchamianie: `uv run scripts/<nazwa>.py` z katalogu głównego projektu.
Sekrety w `.env` (skopiuj z `.env.example`).

## Status

| Skrypt | Sprawdza | Status |
|---|---|---|
| `test_llm_pydantic_ai.py` | pydantic-ai + Claude API + Ollama, ten sam agent/tool na obu | ✅ **Ollama działa** (qwen3:14b, tool calling OK). Claude nie testowany - brak `ANTHROPIC_API_KEY`. |
| `test_intervals_icu.py` | Oficjalne API Intervals.icu (treningi + wellness zsynchronizowane z Suunto) | ✅ **Działa w pełni.** 48 treningów, śr. tętno, dystans, źródło SUUNTO. Wellness: HRV, tętno spoczynkowe, kroki, sen - są. Waga - `None` (Suunto jej nie synchronizuje, potrzebna z Fitdays/Health Connect). |
| `test_telegram_bot.py` | Bot Telegram (echo + odczyt chat_id) | ⏳ Czeka na `TELEGRAM_BOT_TOKEN` (załóż u @BotFather) |
| `test_fitatu.py` | Nieoficjalna biblioteka `fitatu-api`, pobranie dzisiejszego dziennika | ✅ **Działa w pełni** - realny odczyt dnia (posiłki, produkty, makra) na prawdziwym koncie, endpoint w bibliotece 1:1 zgodny z przechwyconym requestem z przeglądarki. |
| `test_suunto.py` | Suunto Cloud API bezpośrednio (apizone) | 🟡 Prawdopodobnie zbędny - Intervals.icu już daje treningi + HRV + sen + tętno spocz. + kroki. Wniosek do apizone nadal warto złożyć (może dać Training Effect/VO2max), ale nie blokuje niczego. |
| ~~`test_healthconnect_webhook.py`~~ | *(usunięty)* Odbiornik webhooka Health Connect | ✅ Zastąpiony właściwą implementacją: `src/health_agent/api/app.py` (`/webhook/healthconnect`) + `src/health_agent/ingest/healthconnect.py` (normalizacja + zapis do Postgresa, idempotentne upserty). Uruchomienie: `uv run uvicorn health_agent.api.app:app --host 0.0.0.0 --port 8000 --ssl-certfile secrets/tailscale.crt --ssl-keyfile secrets/tailscale.key`. Przetestowane end-to-end na realnym payloadzie z telefonu, w tym idempotencja (podwójny import tych samych danych nie duplikuje wierszy). |

## Znaleziska (ważne dla przyszłej architektury)

1. **`fitatu-api` nie jest na PyPI**, mimo że README biblioteki sugeruje
   `pip install fitatu-api`. Zainstalowane bezpośrednio z GitHuba
   (`git+https://github.com/Jezue/fitatu_library`) - w `pyproject.toml`
   trzymać jako zależność git, nie liczyć na PyPI.
2. **`qwen2.5-coder:14b` ma wadliwy tool calling w Ollama 0.32.6** - model
   zwraca JSON wywołania narzędzia jako zwykły tekst zamiast strukturalnego
   `tool_calls` (potwierdzone bezpośrednio przez `POST /api/chat`, nie jest
   to błąd pydantic-ai). **`qwen3:14b` działa poprawnie** i jest teraz
   modelem domyślnym w skrypcie. Do przetestowania osobno: `bielik-11b`.
   Przy wyborze modelu dla realnych agentów - zawsze zweryfikować tool
   calling tym samym sposobem, nie zakładać na podstawie nazwy/rozmiaru modelu.
3. **`FitatuLibrary(session_data=...)`** przyjmuje prosty płaski słownik
   (`bearer_token`, `refresh_token`, `fitatu_user_id`) - nie trzeba pełnego
   formatu Playwright storage-state, co upraszcza ręczne wyciąganie tokenu.
4. **Intervals.icu jako droga na Suunto działa lepiej niż bezpośrednie API
   Suunto.** Oficjalne FAQ Suunto (apizone.suunto.com) mówi wprost, że API
   nie jest dla osób prywatnych - dostęp dostają firmy/organizacje po
   recenzji. Intervals.icu ma za to oficjalną integrację z apką Suunto,
   darmowy klucz API bez żadnego zatwierdzania (Ustawienia -> Developer
   Settings), i realnie daje: treningi (dystans, tętno, tempo, źródło),
   HRV, tętno spoczynkowe, kroki, sen. Autoryzacja to HTTP Basic auth,
   login `"API_KEY"`, hasło = wygenerowany klucz. Jedyny brak: **waga** -
   Suunto jej nie synchronizuje do Intervals.icu, więc waga i tak musi
   przyjść z Fitdays przez Health Connect albo ręcznie z chatu.
5. **Telefon służbowy blokuje sideloading (APK spoza Play) → zmiana
   architektury dla wagi/składu ciała.** `life-dashboard-companion-app`
   (GitHub-only APK) odpada. Zamiast tego: apka **Health Data Export**
   (`com.teqxnology.healthdataexport`, Google Play, open source
   github.com/teqxnology/healthexport) czyta Health Connect i ma tryb
   "Auto Export" co 12/24h **wprost do Google Sheets** - eksportuje
   `Weight, Body fat %, Bone mass, Lean body mass, Height`. Nasz backend
   będzie okresowo czytał ten arkusz przez Google Sheets API (bez
   webhooka, bez publicznego adresu serwera, bez apki spoza Play). Warunek
   wstępny: potwierdzić, że Fitdays faktycznie pisze do Health Connect
   (Ustawienia -> Aplikacje -> Health Connect -> Uprawnienia aplikacji).
   Fitatu prawdopodobnie też ma endpoint do wagi ("Pomiary masy ciała i
   obwodów" w apce) - biblioteka `fitatu-api` go nie implementuje, trzeba
   złapać go ręcznie w DevTools przy okazji wyciągania tokenu.
6. **Potwierdzone na realnym requeście z przeglądarki (curl z DevTools):**
   - Endpoint dnia w bibliotece (`/diet-and-activity-plan/{id}/day/{data}`)
     jest **1:1 zgodny** z prawdziwym requestem - biblioteka nie jest
     przestarzała w tym miejscu.
   - **Bearer token żyje tylko 60 minut** (odczytane z pól `iat`/`exp` w
     samym JWT, zdekodowane lokalnie bez żadnego zapytania na zewnątrz).
     Ręczne wyciąganie tokenu z DevTools nie nadaje się jako stały sposób
     działania - **docelowo używać `FitatuApiClient.login(email, password)`**
     (biblioteka to wspiera), a nie ręcznie kopiować token za każdym razem.
   - Domyślne wartości w bibliotece są nieaktualne względem prawdziwej
     sesji webowej: `app_os` biblioteki to `"WEB"`, realnie przeglądarka
     wysyła `"FITATU-WEB"`; `app_version` biblioteki to `"4.13.1"`, realnie
     `"4.9.1"`; `api_cluster` biblioteki domyślnie `"pl-pl0"`, realnie
     `"pl-pl<user_id>"` (per-user, nie globalny). `test_fitatu.py` teraz
     nadpisuje te trzy pola tak, żeby zgadzały się z prawdziwą sesją -
     zarówno dla poprawności działania, jak i żeby nasz ruch wyglądał
     identycznie jak Twoja normalna przeglądarka (patrz wątek o wykrywaniu
     automatyzacji). User-Agent też podmieniony z uciętego `"Mozilla/5.0"`
     na pełny, realistyczny string Chrome.
   - Konto ma rolę `ROLE_FITATU_PREMIUM_PLUS_AI` - Premium Plus z AI, co
     potencjalnie odsłania też endpointy AI (rozpoznawanie posiłków etc.),
     niesprawdzone jeszcze przez naszą bibliotekę.
7. **Decyzja: zostajemy przy `fitatu-api`, nie piszemy własnej biblioteki.**
   Endpointy się zgadzają, `uv.lock` już przypina dokładny commit (nie
   ruchomy `main`), licencja MIT pozwala forkować w razie potrzeby.
   Podejście: cienka warstwa naszego kodu na wierzchu (poprawne domyślne
   nagłówki + dopisanie brakującego endpointu wagi), nie przepisywanie.
8. **Waga w Fitatu (web) - tylko aktualna wartość, bez historii.** W
   informacjach o koncie jest pole z bieżącą wagą, ale nie ma pod tym
   żadnego endpointu z historią pomiarów (potwierdzone przez użytkownika
   ręcznie w devtools). Możliwe podejście: odpytywać to pole raz dziennie
   i budować historię samemu w naszej bazie (my jesteśmy "pamięcią",
   Fitatu daje tylko snapshot) - ale to *plan B*, najpierw sprawdzamy
   ścieżkę Fitdays -> Health Connect -> Health Data Export -> Sheets,
   która daje prawdziwą historię i pełny skład ciała (tłuszcz, mięśnie,
   kości), nie tylko samą wagę.

9. **DECYZJA: Health Connect zastępuje bezpośrednie API Fitatu jako
   główna ścieżka dla jedzenia.** Potwierdzone przez użytkownika:
   Fitatu zapisuje do Health Connect pełne, szczegółowe dane per produkt
   (nie tylko sumy dzienne) - czyli ta sama jakość danych co z API, bez
   żadnego ryzyka regulaminowego (patrz wcześniejsza dyskusja o ToS i
   wykrywaniu automatyzacji). Kod `fitatu-api` (`scripts/test_fitatu.py`)
   **zostaje w repo, ale jako ścieżka zapasowa** - do włączenia tylko na
   żądanie, gdy potrzebne będą dane "z dzisiaj" zanim dojdzie do nich sync
   (oznaczone jako TODO na później, nieblokujące). Nie usuwamy tego kodu -
   jest przetestowany i działa, może się przydać.
10. **DECYZJA: `Health Connect Webhook` (com.hcwebhook.app) zamiast
   `Health Data Export` + Google Sheets.** Porównanie: `Health Data
   Export`'s towarzyszące repo na GitHubie okazało się **tylko
   dokumentacją**, nie prawdziwym kodem (błędnie wcześniej nazwane "open
   source") - deklaracja "dane nie opuszczają urządzenia" niemożliwa do
   zweryfikowania. `Health Connect Webhook` ma **prawdziwy, kompletny kod
   źródłowy** (186 commitów, 152 gwiazdki, licencja AGPL-3.0) i wysyła
   dane **bezpośrednio na nasz własny webhook**, bez pośrednika (Google
   OAuth, arkusz) - jedna infrastruktura, ta sama co planowana dla
   webhooka Suunto (tunel Tailscale/Cloudflare). Tryby synchronizacji:
   interval (min. 15 min), scheduled (stałe pory, domyślnie 08:00/21:00),
   manual ("Sync Now"). Wymaga Android 14+. Payload: JSON POST, klucze
   `weight` (`kilograms`, `time`), `body_fat` (`percentage`, `time`),
   `nutrition` (`calories`, `protein_grams` itd., `name`, `start_time`,
   `end_time`) - brak wbudowanej autoryzacji w apce, ale można dodać
   własne nagłówki HTTP do webhooka, więc zabezpieczone nagłówkiem
   `X-Webhook-Secret` po naszej stronie. Retry: 3 próby przy błędzie,
   potem czeka na kolejny sync. Lookback tylko 48h - nie robi backfillu
   starszych danych.
11. **Łączność telefon -> serwer rozwiązana przez Tailscale, nie
   Wi-Fi/cloudflared.** WSL nie ma własnego klienta Tailscale domyślnie
   (jest tylko na Windows-hoście) - trzeba było doinstalować `tailscale`
   wewnątrz WSL osobno (`curl -fsSL https://tailscale.com/install.sh | sh`
   + `sudo tailscale up`), co dało WSL własny, stabilny adres w tailnecie
   (`pcdom.tail8242a2.ts.net`, niezależny od zmieniającego się IP WSL).
   Po drodze dwie przeszkody, obie rozwiązane:
   - `tailscale up` wisiał w nieskończoność (log pokazywał powtarzane
     `RegisterReq` bez odpowiedzi) - naprawione przez `tailscale up --reset`.
   - Apka na Androidzie blokowała zwykłe `http://` (cleartext traffic
     policy, standard od Androida 9+), mimo że transport i tak jest
     szyfrowany przez Tailscale - naprawione włączeniem **HTTPS
     Certificates** w panelu Tailscale (login.tailscale.com/admin/dns) i
     wygenerowaniem prawdziwego certu przez `tailscale cert` (wymaga
     `sudo tailscale set --operator=$USER` raz, żeby nie trzeba było sudo
     przy każdym użyciu). Serwer w `test_healthconnect_webhook.py` sam
     wykrywa obecność `secrets/tailscale.crt`/`.key` i włącza HTTPS.
   - Po drodze też: nazwa MagicDNS (`*.ts.net`) chwilowo nie rozwiązywała
     się na telefonie - typowa przyczyna to ustawienie systemowe
     **Private DNS** na Androidzie (Ustawienia -> Sieć -> Prywatny DNS)
     ustawione na konkretnego dostawcę zamiast "Automatycznie" - to
     omija DNS wypychany przez VPN Tailscale. Warto o tym pamiętać przy
     kolejnych apkach na telefonie korzystających z nazw `*.ts.net`.

## Następne kroki

1. Uzupełnij `.env` (Telegram token, ANTHROPIC_API_KEY) i odpal pozostałe
   skrypty - każdy da się przetestować niezależnie. Fitatu, Health Connect
   Webhook i Intervals.icu już gotowe i potwierdzone na prawdziwych danych.
2. Ustaw w apce "Health Connect Webhook" stały tryb synchronizacji
   (interval min. 15 min, albo scheduled) zamiast tylko ręcznego "Sync Now".
3. **[W TOKU - na użytkowniku]** Przetestuj synchronizację Fitatu ->
   Health Connect dla posiłków z przyszłych dni (już potwierdzone że
   działa dla dnia, w którym robiono test).
4. Zgłoś się na apizone.suunto.com jak najszybciej (3-4 tyg. oczekiwania,
   niepewne zatwierdzenie) - ale to już tylko "nice to have" (Training
   Effect/VO2max), nie blokuje niczego, bo Intervals.icu już działa.
5. **Wszystkie trzy źródła danych (Suunto/Intervals.icu, Fitatu/Health
   Connect, waga/skład ciała) są teraz potwierdzone i działające** -
   Faza 0 (smoke testy) w praktyce ukończona. Następny logiczny krok to
   Faza 1 z pełnego planu: szkielet bazy danych + normalizacja i zapis
   tego co już umiemy pobrać.

**Aktualizacja - Faza 1 zakończona, Faza 2 zakończona:** szkielet bazy
(Postgres w Dockerze, SQLAlchemy 2 + Alembic) i pełna ingestia Health
Connect (waga, skład ciała, odżywianie) oraz Intervals.icu (treningi,
wellness) są gotowe w `src/health_agent/`, przetestowane end-to-end na
prawdziwych danych, łącznie z idempotencją (podwójne uruchomienie nie
duplikuje wierszy) i obsługą duplikatów w ramach jednej paczki danych
(dwie identyczne pozycje jedzenia tego samego dnia = dwa osobne wiersze,
nie nadpisanie). Naprawiony po drodze realny błąd: kolejność zapisu
łamała klucz obcy między pozycjami jedzenia a dniem.

Potwierdzone ograniczenia źródła Intervals.icu (dane z Suunto): brak
Training Effect i VO2max na poziomie treningu, brak podziału snu na fazy
(deep/rem/light - tylko łączny czas i wynik), brak calories_active/total
w endpoincie wellness. To są realne braki tej ścieżki względem
bezpośredniego API Suunto (Faza 4, opcjonalna), nie błędy naszego kodu.

Zapasowa ścieżka Fitatu (`fitatu-api`) nadal działa tylko jako osobny
skrypt (`test_fitatu.py`) - to zamierzone, bo jest to ścieżka używana
rzadko/na żądanie, nie stały pipeline (patrz pkt 9 wyżej).

**Automatyczny polling:** `src/health_agent/scheduler.py` (APScheduler,
działa w tle w tym samym procesie co serwer FastAPI - patrz `lifespan` w
`api/app.py`) odpytuje Intervals.icu co godzinę, oknem 3 dni wstecz (nie
tylko "dziś" - Intervals.icu potrafi doliczać/poprawiać dane wstecznie).
Potwierdzone działanie: log pokazuje start schedulera, natychmiastowy
pierwszy przebieg przy starcie serwera, i poprawnie zaplanowany kolejny za
godzinę. Ręczne pobranie nadal dostępne przez `health-agent ingest
intervals --since ...` - i to, i scheduler, i przyszłe narzędzie agenta
(Faza 3) będą wołać dokładnie tę samą funkcję `ingest_range`, więc mogą się
bezpiecznie nakładać (idempotentne upserty).

**Aktualizacja - Faza 3 w toku (agenci + Telegram):** zbudowane w
`src/health_agent/agents/` (orchestrator + 5 specjalistów: running,
strength, nutrition, body, recovery), `tools/` (odczyt z bazy dla każdej
dziedziny), oraz bot na Telegramie (`channels/telegram.py`). Realne błędy
złapane i naprawione po drodze:
- `Agent.run_sync()` nie da się wywołać wewnątrz narzędzia innego,
  już działającego agenta (delegate/ask_agent) - deadlock. Naprawione
  przez rozdzielenie `run_agent` (async, do użycia w narzędziach) i
  `run_agent_sync` (do wejścia z zewnątrz - CLI). Ten sam błąd w drugą
  stronę złapany w handlerze Telegrama (sam jest `async def` w pętli
  zdarzeń bota) - naprawione osobnym `ask_orchestrator_async`.
- `result.usage()` to w tej wersji pydantic-ai atrybut, nie metoda.
- Lokalne modele (qwen3:14b) zawodnie łączą wymuszony structured output
  (`AgentAnswer` jako JSON) z tool-callingiem w zagnieżdżonych wywołaniach
  - model "widział" że powinien wywołać narzędzie (log thinking), ale
  zamiast tego wypisywał placeholder pasujący do schematu. Naprawione:
  agenci "liściowi" (cel `ask_agent`) zwracają zwykły tekst, `AgentAnswer`
  budowany po stronie kodu, nie przez model.
- Orchestrator na krótkie, proste pytania ("ile kroków wczoraj?") czasem
  w ogóle pomijał wywołanie `delegate`, mimo instrukcji w prompcie -
  naprawione przez dużo bardziej stanowczy prompt z konkretnym
  przykładem. To wciąż jakościowo słabszy routing niż dałby Claude -
  udokumentowane jako znane ograniczenie modelu lokalnego, nie błąd kodu.

Drzewo kosztów (`agent_runs`, parent_run_id) działa poprawnie - sprawdzone
na żywo dla scenariusza orchestrator -> running -> (recovery + nutrition).

**Dwa kolejne realne błędy złapane na żywym Telegramie (po pierwszym udanym teście):**
- **Zły ruting specjalisty**: pytanie "ostatni trening" trafiło do `strength`
  zamiast `running` - opisy specjalistów w prompcie orchestratora były
  niejednoznaczne ("trening" kojarzone z siłownią). Naprawione precyzyjniejszym
  opisem ról.
- **Konfabulacja danych zdrowotnych** (poważniejsze): `get_workouts(days=1)`
  zwróciło pustą listę (zły dobór okna przez model), model wymyślił fałszywe
  `external_id`, dostał `None` z `get_workout_detail`, i MIMO TO wygenerował
  pełny, szczegółowy, całkowicie zmyślony raport (dystans, tętno, strefy,
  nawet temperaturę ciała - której w ogóle nie ma w schemacie). Naprawione
  dwutorowo: (1) nowe narzędzie `get_latest_workout()` zwracające najnowszy
  trening bez potrzeby zgadywania okna czasowego, (2) dużo ostrzejsze zasady
  w prompcie - jawny zakaz podawania wartości niepochodzących z wyniku
  narzędzia, z wyjaśnieniem dlaczego to groźniejsze niż brak odpowiedzi.
  Po poprawce: te same pytanie dało w 100% zgodne z bazą dane.
- **Brak historii rozmowy** - każde pytanie do orchestratora leciało jako
  oderwana wiadomość, bez kontekstu poprzednich wymian w tym samym czacie.
  Naprawione: `channels/telegram.py` wczytuje ostatnie 6 wiadomości z
  `conversations` i dokleja je jako tekstowy kontekst (`_with_history` w
  `registry.py`) - działa niezależnie od modelu, bez serializacji
  wewnętrznego formatu wiadomości pydantic-ai.
- **Brak świadomości "dzisiejszej daty"** - model ocenił prawdziwą datę
  treningu (2026-09-15) jako "z przyszłości" i odmówił podania danych,
  bo jego wewnętrzne założenie "dziś" jest wcześniejsze niż dane z tego
  roku. Naprawione: `build_agent()` (wołane od nowa przy KAŻDEJ
  wiadomości, nie raz przy starcie) dokleja `dzisiejsza data: ...` na
  początek promptu - aktualizuje się samo dzień po dniu, bez restartu bota.

Po tej serii poprawek: ten sam scenariusz ("szczegóły ostatniego
treningu") zwraca poprawne, w pełni zgodne z bazą dane, łącznie z
interwałami z surowego JSON-a Intervals.icu. Pozostała drobna
niedoskonałość - model czasem błędnie przelicza jednostki (sekundy na
tempo min/km) - to nieszkodliwa pomyłka arytmetyczna, nie zmyślanie
danych, zaakceptowana jako znane ograniczenie modelu lokalnego.

**Kolejna runda znalezisk (analiza pytania "ile spaliłem kalorii" na żywym Telegramie):**
- **Formatowanie Markdown w ogóle się nie renderowało** - `reply_text()`
  bez `parse_mode` wysyła zwykły tekst, gwiazdki widoczne dosłownie.
  Naprawione: `parse_mode=ParseMode.MARKDOWN` (legacy, bardziej
  wyrozumiały niż V2) z fallbackiem na zwykły tekst przy błędzie parsowania.
- **Niezgodność składni pogrubienia**: model generuje standardowy
  `**pogrubienie**`, ale Telegram (oba tryby Markdown) oczekuje
  POJEDYNCZEJ gwiazdki - sprawdzone bezpośrednio przez API (`entities`
  w odpowiedzi): `**x**` Telegram po cichu ignoruje (nie błąd, po prostu
  nie pogrubia), `*x*` poprawnie tworzy encję "bold". Naprawione regexem
  konwertującym `**x**` -> `*x*` przed wysyłką.
- **"Kalorie" domyślnie kojarzone z jedzeniem, nie ze spalaniem** - pytanie
  "ile spaliłem kalorii" trafiało do `nutrition` (dziennik jedzenia) zamiast
  `running`. Ciekawy szczegół: odpowiedź akurat wyszła poprawna, bo model
  odzyskał prawdziwą liczbę z historii rozmowy (dodanej kilka poprawek
  wcześniej) - ale to przypadek, nie poprawny mechanizm, bo `nutrition` w
  ogóle nie ma narzędzia do danych treningowych. Naprawione jaśniejszym
  opisem ról w prompcie.
- **Brak całodniowego wydatku kalorycznego jako osobna, realna luka
  danych** (nie błąd promptu) - `daily_activity.calories_active/total` są
  ZAWSZE puste, bo Intervals.icu tego nie udostępnia. Pierwsza poprawka
  rutingu dawała kalorie z treningu jako mylącą odpowiedź na pytanie o
  cały dzień. Naprawione: prompt `running` explicit rozróżnia oba pojęcia
  i uczciwie mówi o braku danych całodniowych zamiast je podmieniać.
- **`get_workouts(days=N)` zawodny dla pytań o konkretny dzień** ("wczoraj")
  - model raz w ogóle nie wywołał żadnego narzędzia i od razu stwierdził
  brak danych (mimo że dane były). Naprawione nowym narzędziem
  `get_workouts_on_date(date)` - model przelicza "wczoraj" na konkretną
  datę (ma dzisiejszą datę w promptcie) zamiast zgadywać liczbę dni wstecz.

**Sprostowanie i realna droga na całodniowe kalorie:** wcześniej błędnie
założyłem, że Suunto jest już połączone z Health Connect na telefonie -
nieprawda, użytkownik potwierdził że tam widnieją tylko Fitdays, Strava i
Fitatu. Sprawdzona alternatywa: apka **Health Sync** (appyhapps, darmowy
tydzień próbny, potem jednorazowa opłata lub subskrypcja) oficjalnie
obsługuje Suunto jako źródło i Health Connect jako cel - ale dokładny
zestaw przenoszonych danych (czy total_calories, czy tylko active/steps)
nie jest nigdzie publicznie opisany, apka sama pokazuje to dopiero po
podłączeniu. Do zweryfikowania przez użytkownika w darmowym okresie
próbnym, zanim cokolwiek zapłaci.

Niezależnie od wyniku tego testu, `ingest/healthconnect.py` już teraz
obsługuje `steps`/`active_calories`/`total_calories` z dowolnego źródła
Health Connect (upsert biorący MAX z dnia - kolejne synchronizacje w ciągu
dnia raportują rosnącą wartość, MAX unika cofania sumy). Po drodze
naprawiony realny bug: `tools/recovery.py` brał tylko JEDEN wiersz
`daily_activity` na dzień, mimo że teraz mogą być dwa (różne źródła:
intervals_icu i healthconnect) - po cichu gubiłoby dane jednego źródła.
Naprawione przez scalanie pól ze wszystkich źródeł per dzień zamiast
brania losowego jednego wiersza.

**Zamknięcie sprawy całodniowych kalorii z Suunto:** użytkownik sprawdził
Health Sync bezpośrednio (screenshot listy typów danych) - Suunto ma
strzałkę (czyli faktycznie coś eksportuje) tylko dla: Kroki, Aktywności,
Sen, Tętno, Nasycenie tlenem. Brak jakiejkolwiek kategorii "Kalorie".
Ostatecznie potwierdzone: Suunto NIE udostępnia całodniowego wydatku
kalorycznego żadną znaną nam drogą (ani Intervals.icu, ani Health Connect
przez Health Sync). Kod do obsługi steps/active_calories/total_calories w
`ingest/healthconnect.py` zostaje (przyda się dla innych źródeł, gdyby
się pojawiły), ale nie ma obecnie niczego co by go realnie zasilało.

Decyzja użytkownika: zamiast dalej szukać źródła, docelowo albo ręczne
wpisywanie przez chat, albo (później) szacowanie na podstawie kalorii z
treningów + liczby kroków. Żadne z tego nie zaimplementowane jeszcze -
do zrobienia w Fazie 3 (ręczne wpisy) / ew. dalszej iteracji.

**Ręczne wpisy (`log_manual_entry`) podpięte pod orchestratora**, zgodnie
z pierwotnym planem. Rozszerzone o `kind="daily_calories"` (odpowiedź na
brak tej danej z żadnego automatycznego źródła - patrz wyżej) - zapisuje
się do `manual_logs` (audyt) i `daily_activity` (source="manual",
`calories_total`), zwykłym nadpisaniem, nie MAX jak przy Health Connect
(to świadomy, jednorazowy wpis, nie rosnący licznik z urządzenia).
Przetestowane end-to-end: wpis "Spaliłem dziś 2380 kalorii" poprawnie
zapisany i poprawnie odczytany przez `recovery` w kolejnym pytaniu.
Drobna nieścisłość modelu przy odczycie: błędnie podpisał ręczny wpis
jako "dane z Health Connect" (liczba poprawna, tylko źle przypisane
źródło) - kosmetyczne, niezaadresowane.

**Realny bug znaleziony przez użytkownika: duplikaty jedzenia między
kolejnymi synchronizacjami przez szum zmiennoprzecinkowy.** Użytkownik
zrobił drugi "Sync Now" tego samego dnia (15.09) i zauważył, że suma
kalorii (2575.89) nie zgadzała się z oczekiwaną (2075). Przyczyna:
mechanizm dedup pozycji jedzenia (`_nutrition_external_id`) haszował
SUROWE wartości kcal/protein - te same realne produkty ("Kajzerka",
"Schab tradycyjnie wędzony") wracały z Fitatu/Health Connect przy
kolejnym syncu z mikroskopijnie różnymi wartościami zmiennoprzecinkowymi
(336 vs 336.00000000000006 - szum z przeliczeń, nie różnica w ilości),
więc dostawały RÓŻNY hash i duplikowały się zamiast się nadpisać.
Naprawione dwuetapowo (drugi etap złapany dopiero w teście pierwszej
poprawki): (1) zaokrąglenie kcal/protein do 1 miejsca po przecinku przed
haszowaniem, (2) wymuszenie `float()` PRZED `round()` - JSON bez kropki
dziesiętnej parsuje się w Pythonie jako int, więc `round(int, 1)` i
`round(float, 1)` dawały różne reprezentacje tekstowe ("336" vs "336.0")
nawet dla tej samej zaokrąglonej wartości. Zweryfikowane: identyczne
produkty z różnym szumem FP teraz kolidują poprawnie w jeden wpis,
genuinely różne porcje tego samego produktu (różnica >0.1 kcal) nadal
dostają osobne wpisy. Dane historyczne dla 15.09 wyczyszczone ręcznie i
przeliczone (2074.54 kcal, zgodne z oczekiwaniem użytkownika).

**Faza 3 (Claude, orkiestracja) - konfiguracja finalna i optymalizacja
kosztu (2026-09-16).** Po podłączeniu prawdziwego `ANTHROPIC_API_KEY`
(zwrócić uwagę: `pydantic-settings` czyta `.env` tylko do obiektu
`Settings`, NIE eksportuje do `os.environ` - string typu
"anthropic:claude-opus-5" jako `model` w `Agent()` każe pydantic-ai
budować domyślny `AnthropicProvider()`, który czyta klucz WYŁĄCZNIE z
`os.environ` i wybucha mimo poprawnego `.env` - trzeba jawnie
`AnthropicProvider(api_key=settings.anthropic_api_key)`, patrz
`agents/base.py:_build_anthropic_model`) zrobiono serię zmierzonych
optymalizacji, w tej kolejności:
1. `scripts/eval_agents.py` - 12 "złotych" pytań + test jednostkowy
   dedupu `log_manual_entry`, asercje na drzewo `agent_runs` i efekty w
   bazie (NIE ocenia jakości merytorycznej tekstu - to nadal wymaga
   człowieka). Punkt odniesienia do każdej kolejnej zmiany.
2. Prompt caching Anthropic (`anthropic_cache_instructions`,
   `anthropic_cache_tool_definitions` w `AnthropicModelSettings`) - -29%
   kosztu testu, zweryfikowane przez `cache_read_tokens` w `Usage`, nie
   założone. Haiku ma wyższy minimalny próg cache'owanego prefiksu niż
   Sonnet - orchestrator (krótki prompt) słabiej korzysta, ale to i tak
   najtańszy agent.
3. Węższe narzędzie `get_recovery_day(date)` obok `get_recovery_range`
   (analogicznie do już istniejącego `get_workouts_on_date`) - 622 → 41
   tokenów na pytanie o jeden dzień. Bez wymiernego wpływu na koszt
   całego pytania (system prompt+narzędzia dominują), ale mniejsze
   ryzyko, że model wybierze zły wiersz z wielodniowej listy.
4. Output functions pydantic-ai (`output_type=[str, _delegate,
   log_manual_entry]` zamiast `tool_plain`) - orchestrator kończy run
   OD RAZU wynikiem delegacji/zapisu, bez drugiego wywołania modelu na
   parafrazę. -27% kosztu, ~30% mniej latencji. Złapane po drodze:
   `log_manual_entry` zwracała samo `int` (id) - jako output function
   ta wartość staje się FINALNĄ odpowiedzią użytkownika, więc bez
   zmiany user zobaczyłby gołe "31" zamiast potwierdzenia. Naprawione:
   `log_manual_entry` teraz formatuje deterministyczne potwierdzenie po
   polsku (`_format_confirmation`), logika zapisu/dedupu przeniesiona do
   `_write_manual_log`.
5. Haiku 4.5 zamiast Sonnet 5 dla `nutrition`/`recovery` - zweryfikowane
   3x pełnym przebiegiem `eval_agents.py` (39/39 PASS za każdym razem,
   w tym najtrudniejszy test: recovery samo konsultuje nutrition przy
   pytaniu o deficyt, bez pytania użytkownika o zgodę). `running`
   zostaje na Sonnet 5 - jedyny agent robiący złożoną korelację
   (trening × regeneracja × odżywianie), niesprawdzony na Haiku.

Łącznie: $0.1858 → ~$0.083 za identyczny zestaw testów (-55%), 0
regresji po drodze (każdy krok zweryfikowany pełnym przebiegiem
`eval_agents.py` przed przejściem dalej).

**Alerty i backup (2026-09-16).** Dodane do istniejącego
`BackgroundScheduler` z `scheduler.py` (ten sam proces co polling
Intervals.icu - żyje w `api/app.py`, NIE w procesie bota Telegram;
jeśli kiedyś proces webhooka przestanie działać ciągle, te joby też
przestaną się odpalać, warto o tym pamiętać przy Fazie 6).
- `check_stale_sources`: sprawdza `MAX(started_at)`/`MAX(measured_at)`/
  `MAX(date)` dla workouts/body_composition/nutrition_days, alert na
  Telegram (bezpośrednio przez `telegram.Bot`, bez potrzeby bycia
  "wewnątrz" Application) jeśli źródło martwe > `ALERTS_STALE_HOURS`.
  Dedup: jeden alert/źródło/dzień, zapamiętany w `agent_memory` pod
  pseudo-agentem `"system_alerts"` (bez tego alert leciałby co godzinę
  dla martwego źródła). Zweryfikowane end-to-end live (realna wiadomość
  doszła na Telegram przy wymuszonym `alerts_stale_hours=0`).
- `backup_database`: `docker compose exec -T db pg_dump -U health_agent
  health_agent` (NIE lokalny `pg_dump` - niezainstalowany na hoście w
  tym środowisku, i tak Postgres żyje tylko w Dockerze) -> gzip ->
  `backups/<timestamp>.sql.gz` (katalog dodany do `.gitignore` - to
  realne dane zdrowotne, nie mogą trafić do repo). Retencja: usuwa
  pliki starsze niż `BACKUP_RETENTION_DAYS`. Zweryfikowane: prawdziwy
  dump, 92KB, poprawny SQL po `zcat`.
- Wszystko konfigurowalne przez `Settings`/`.env`
  (`ALERTS_ENABLED/STALE_HOURS/CHECK_INTERVAL_MINUTES`,
  `BACKUP_ENABLED/DIR/INTERVAL_HOURS/RETENTION_DAYS`), z sensownymi
  domyślnymi wartościami - `.env.example` udokumentowany.
- Poranny/wieczorny raport (reszta Fazy 5) świadomie pominięty na razie
  na prośbę użytkownika.
