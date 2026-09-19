# Instrukcje dla agentów pracujących nad health-agent

Wspólne dla Codex i Claude Code (import przez `CLAUDE.md`).

## Start pracy

- Komunikuj się z użytkownikiem po polsku. Zachowuj istniejący styl kodu.
- Przeczytaj `README.md` i `docs/architecture.md`; odpowiednie części `TODO.md` i `PLAN.md` dobieraj do zadania.
- Sprawdź `git status --short` i istotny diff. Zachowuj istniejące zmiany użytkownika i drugiego agenta; nie cofaj ich ani nie nadpisuj.
- Źródłem prawdy o implementacji jest kod i migracje. `PLAN.md` opisuje przyszłe zmiany, a `scripts/README.md` jest historią, nie aktualnym runbookiem.
- Nie wykonuj backlogu bez związku z bieżącym zleceniem. Nie zakładaj dostępu do historii czatu ani prywatnego `~/.claude/plans/`.
- Tylko Codex: przed doborem modeli/delegacji przeczytaj `docs/codex-workflow.md` i stosuj zawarte tam reguły automatycznej delegacji. Claude Code korzysta ze wspólnych zasad tego pliku, ale nie z ról ani modeli `.codex/`.

## Środowisko i weryfikacja

- Linux/WSL, Python >=3.12, `uv`, PostgreSQL 16 w Docker Compose. Komendy wykonuj z katalogu głównego repo w WSL; z Windows: `wsl.exe -d Ubuntu-24.04 --cd /home/<uzytkownik>/health-agent -- <komenda>`.
- Korzystaj z `uv` i `uv.lock`; nie zastępuj środowiska WSL windowsowym `.venv` i nie aktualizuj zależności przy okazji innego zadania.
- Szybka kontrola: `uv run --frozen --no-sync python -m compileall -q src scripts alembic` i `uv run --frozen --no-sync health-agent --help` (wymagają istniejących zależności).
- Testy jednostkowe uruchamiaj przez `python -m unittest discover -s tests`;
  repo nie ma konfiguracji `pytest` ani CI. Skrypty `scripts/test_*.py` są
  integracyjne; sprawdź ich wejście i skutki uboczne przed uruchomieniem.
- `scripts/eval_agents.py` wywołuje modele, zapisuje dane i USUWA CAŁĄ tabelę `agent_runs`. Używaj wyłącznie osobnej bazy testowej przez `DATABASE_URL`, z migracjami i danymi syntetycznymi. Tryb `all` uruchamia obecnie tylko core + running; coaches i import uruchamiaj osobno.
- Zmiany promptów, routingu, modeli i narzędzi weryfikuj odpowiednimi zestawami eval na bazie testowej; obliczenia/dedup także deterministycznymi przypadkami. Podaj wykonane kontrole i niewykonane testy wraz z powodem.
- Start API uruchamia zadania okresowe tylko przy `SCHEDULER_ENABLED=true`;
  wtedy polling i domyślny backup wykonują się od razu, później także alerty.
  Nie uruchamiaj drugiego aktywnego schedulera ani bota z tokenem production
  do sprawdzenia importu. Nie używaj wielu workerów API przy schedulerze.

## Reguły implementacji

- Licz agregaty, trendy i jednostki w Pythonie (`tools/`); LLM interpretuje wyniki. Brak danych nie oznacza zera i nie wolno uzupełniać go wymyślonymi pomiarami.
- Zachowuj audyt w `raw_payloads` i idempotencję danych znormalizowanych. Przy zmianie ingestii sprawdź ponowny import, identyczne pozycje żywieniowe i szum float.
- Zmiany schematu: modele SQLAlchemy + nowa migracja Alembic. Nie zmieniaj zastosowanych migracji; weryfikuj na osobnej bazie.
- W działającej pętli asyncio używaj `await`; wejście synchroniczne jest dla CLI. Konsultacja specjalistów pozostaje ograniczona przez leaf agent bez `ask_agent`.
- Modele konfiguruje `config/agents.yaml`, metodologia jest w `src/health_agent/prompts/`, wspólny kontrakt i routing w `src/health_agent/agents/registry.py`.
- Rozróżniaj profil (`agent_memory`, `user_profile`), wiedzę (`knowledge`) i oryginały notatek (`documents`). `agent_memory` służy też do deduplikacji alertów i automatycznych podsumowań.
- Nie odczytuj ani nie wypisuj `.env`, `secrets/`, backupów i rzeczywistych danych zdrowotnych bez potrzeby wynikającej z zadania. Do dokumentacji/testów używaj syntetycznych przykładów; nie commituj sekretów ani danych użytkownika.
- Utrzymuj decyzję użytkownika: planowane zdjęcia sylwetki służą archiwum i porównaniu, nie analizie przez model ani wysyłce do API LLM. Raporty dzienne i tygodniowe są opcjonalne i domyślnie wyłączone; decyzję o raportach dziennych użytkownik zmienił 2026-09-19.

## Utrzymywanie kontekstu i przekazanie pracy

- Aktualizuj dokumentację w tej samej zmianie co kod: komendy/setup w `README.md`, architekturę i pułapki w `docs/architecture.md`, stan zadań w `TODO.md`, zmienione kroki wdrożenia w `PLAN.md`.
- `AGENTS.md` zmieniaj przy trwałej zmianie zasad pracy. `CLAUDE.md` pozostaw cienkim importem; nie duplikuj tam kontekstu.
- Usuwaj nieaktualne twierdzenia zamiast dopisywać sprzeczne instrukcje. Nie dopisuj dziennika każdej rozmowy ani pomiarów użytkownika do plików instrukcji.
- Przy przerwanym większym zadaniu dopisz pod odpowiednim punktem `TODO.md`: stan, zmienione pliki/branch, weryfikacja, blokada i następny krok. Po zakończeniu usuń nieaktualną notatkę.
- Na wspólnym checkoutcie pracujcie kolejno; do równoległej edycji używajcie osobnych worktree/branchy. Wspólne pliki nie oznaczają wspólnej historii rozmowy ani izolacji bazy i procesów.
