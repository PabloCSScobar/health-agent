# Workflow Codex: modele i automatyczna delegacja

Dotyczy agentów programistycznych Codex, nie agentów zdrowotnych aplikacji
w `config/agents.yaml`. Claude Code nadal czyta wspólne `AGENTS.md` przez
`CLAUDE.md`; nie ma obowiązku ani możliwości używania tych ról OpenAI
na podstawie samej konfiguracji `.codex/`.

## Instrukcje dla głównego agenta Codex

Użytkownik zleca automatyczne delegowanie według poniższych reguł.
Nie pytaj ponownie o sam wybór roli/modelu. Uprawnienia do operacji na
danych, procesach i usługach pozostają ograniczone bieżącym zleceniem.

- Główny agent: domyślnie Terra / medium. Sam rozpoznaje, implementuje,
  pisze testy i poprawia błędy. Nie twórz drugiego implementera tylko po
  to, by przekazać mu całość tego samego zadania.
- Drobna dokumentacja, kosmetyka lub mechaniczna zmiana: pracuj sam,
  wykonaj adekwatne sprawdzenie. Bez obowiązkowej konsultacji/review.
- Normalna funkcja lub poprawka zachowania: ustal kryteria odbioru,
  implementuj i testuj, potem zleć niezależną weryfikację roli `verifier`
  (Terra / high). Napraw potwierdzone błędy i powtórz dotknięte kontrole.
- Przy zmianie migracji, autoryzacji, współbieżności, transakcji,
  deduplikacji lub możliwości utraty danych wybierz `risk-reviewer`
  (Sol / high) zamiast zwykłego weryfikatora, nie obu automatycznie.
- Wywołaj `architect` (Astra / high) przed implementacją, gdy trzeba
  rozstrzygnąć istotny kompromis architektoniczny, migrację z ryzykiem
  utraty danych albo sprzeczne wymagania techniczne. Konsultuj też po
  dwóch próbach rozwiązania tego samego problemu bez nowej wiedzy lub
  na wyraźną prośbę użytkownika. Prosty plan nie wymaga Astry.
- Konsultacja Astry kończy się decyzją/planem; wykonanie wraca do głównego
  agenta. Nie konsultuj ponownie tej samej decyzji bez nowych dowodów.
  Sprzecznych celów produktowych nie rozstrzygaj za użytkownika.
- `repo-explorer` (Luna / medium) jest opcjonalny: używaj do konkretnego,
  niezależnego odczytu, gdy delegacja oszczędza czas/kontekst. Przy małym
  wyszukiwaniu sprawdź kod sam. Nie uruchamiaj wszystkich ról do każdego zadania.
- Przed delegacją krótko podaj rolę/model i powód. Używaj zdefiniowanej
  roli, nie domyślnego workera bez jawnego doboru modelu i reasoning.
- Deleguj konkretne, ograniczone zadanie, gdy równolegle masz użyteczną
  pracę lokalną (np. ustalenie kryteriów odbioru, testy, dokumentację).
  Nie edytuj rozwiązania zależnego od nieukończonej konsultacji.
- Maksymalnie dwóch pomocników jednocześnie. Pomocnicy nie delegują dalej.
  Na wspólnym checkoutcie jeden właściciel zmian kodu; weryfikator może
  dopisać test tylko po przydzieleniu mu konkretnych plików.
- Jeśli klient nie udostępnia roli/modelu, powiedz o ograniczeniu.
  Nie udawaj konsultacji i nie zastępuj Astry tańszym modelem po cichu.
  Kontynuuj niezależne bezpieczne kroki; brak review oznacz jako NOT RUN.

## Pakiet przekazania i warunek zakończenia

Subagent dostaje: cel, zakres, kryteria odbioru, istotne pliki/symbole,
zakres diffu (w tym pliki nieśledzone), wykonane kontrole i otwarte pytanie.
Przekazuj potrzebny kontekst, nie całą historię rozmowy. Weryfikator ma
sam sprawdzić kod, nie tylko zaakceptować podsumowanie implementera.

Wynik weryfikacji zawiera polecenia i rezultaty, przypadki brzegowe,
reprodukcję usterek oraz brakujące kontrole. NOT RUN nie jest PASS.
Implementer odpowiada za testy również przed review. Testy bazy wymagają
osobnej bazy; `scripts/eval_agents.py` usuwa całe `agent_runs`.
Ta konfiguracja nie tworzy środowiska testowego ani nie naprawia skryptów eval.

## Aktywacja i ograniczenia

Pliki `.codex/config.toml` i `.codex/agents/*.toml` są projektowe i można
wersjonować je razem z repo. Otwórz nową sesję w tym repo po zapisaniu
konfiguracji. Używaj zaufanego projektu; klient może pomijać konfigurację
niezaufanego repo. Jawny wybór modelu w UI/CLI może nadpisać domyślną Terrę.
Istniejąca rozmowa na Astrze nie przełączy się automatycznie na Terrę.
Sprawdź selektor modelu w aplikacji lub `/model` w CLI; jeżeli istniejący
wybór jest zachowany, wybierz jednorazowo Terra / medium.

Codex CLI w WSL musi być zainstalowany i zalogowany osobno. W chwili
przygotowania workflow `codex` nie był dostępny w sprawdzonym PATH WSL.
Konfiguracja repo nie instaluje CLI ani nie synchronizuje kont Windows/WSL.
Po instalacji startuj z głównego katalogu projektu. Jawny wybór domyślnego
modelu w CLI, jeśli potrzebny:

```bash
codex -m gpt-5.6-terra -c model_reasoning_effort=medium
```

Smoke test w nowej sesji: poproś o krótkie wskazanie miejsca definiowania
komend CLI przez `repo-explorer`, bez zmian i usług. Sprawdź w aktywności
subagenta, czy użył Luny, i czy główny model pozostał Terrą. Sam parser TOML
nie dowodzi załadowania ról przez UI ani działania delegacji z kontem użytkownika.

Reguły są instrukcjami dla modelu, nie deterministycznym routerem kosztowym.
Konkretne modele ról są przypięte w TOML, lecz rozpoznanie rodzaju zadania
pozostaje decyzją głównego agenta. Nie stosujemy Max/Ultra domyślnie.
Po kilkunastu zadaniach oceń czas do akceptacji, liczbę poprawek i zużycie,
zamiast optymalizować wyłącznie cenę pojedynczego wywołania.

Źródło formatu i zasad delegacji (sprawdzone 2026-09-18):
[OpenAI: Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
