"""
Smoke test: oficjalne API Intervals.icu - pobranie ostatnich treningów
(zsynchronizowanych z Suunto) i danych "wellness" (waga, HRV, tętno
spoczynkowe, kroki - jeśli je tam wpisujesz/synchronizujesz).

To jest droga na dane z Suunto BEZ czekania na zatwierdzenie apizone.suunto.com
(które wg oficjalnego FAQ Suunto w ogóle nie jest dla osób prywatnych) -
Intervals.icu ma oficjalną integrację z aplikacją Suunto i darmowe API
z osobistym kluczem, bez żadnego zatwierdzania.

Jak zdobyć dane logowania:
    1. Zaloguj się na intervals.icu -> Settings -> Developer Settings
       -> Generate API key.
    2. ID zawodnika masz w URL profilu (np. i713159) albo tam samo obok klucza.
    3. Wklej do .env jako INTERVALS_API_KEY / INTERVALS_ATHLETE_ID.

Autoryzacja: HTTP Basic auth, login "API_KEY", hasło = Twój klucz
(oficjalnie udokumentowany sposób dla użytku osobistego, patrz
https://forum.intervals.icu/t/api-access-to-intervals-icu/609).

Użycie:
    uv run scripts/test_intervals_icu.py
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://intervals.icu/api/v1"


def main() -> None:
    api_key = os.environ.get("INTERVALS_API_KEY")
    athlete_id = os.environ.get("INTERVALS_ATHLETE_ID")

    if not api_key or not athlete_id:
        print(
            "⚠️  Brak INTERVALS_API_KEY / INTERVALS_ATHLETE_ID w .env - "
            "patrz instrukcja w docstringu tego skryptu."
        )
        return

    auth = ("API_KEY", api_key)
    newest = date.today()
    oldest = newest - timedelta(days=30)

    print(f"=== Treningi z ostatnich 30 dni ({oldest} - {newest}) ===")
    resp = requests.get(
        f"{BASE_URL}/athlete/{athlete_id}/activities",
        params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        auth=auth,
        timeout=15,
    )
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"Treść odpowiedzi: {resp.text[:500]}")
    else:
        activities = resp.json()
        print(f"Znaleziono {len(activities)} treningów.")
        for act in activities[:5]:
            print(
                f"  - {act.get('start_date_local', '?')[:10]} "
                f"{act.get('type', '?'):12s} "
                f"{(act.get('distance') or 0) / 1000:.1f} km  "
                f"{act.get('moving_time', 0) // 60} min  "
                f"śr. tętno: {act.get('average_heartrate', '?')}  "
                f"źródło: {act.get('source', '?')}"
            )

    print(f"\n=== Wellness z ostatnich 30 dni ===")
    resp = requests.get(
        f"{BASE_URL}/athlete/{athlete_id}/wellness",
        params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        auth=auth,
        timeout=15,
    )
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"Treść odpowiedzi: {resp.text[:500]}")
    else:
        wellness = resp.json()
        print(f"Znaleziono {len(wellness)} dni z danymi wellness.")
        for day in wellness[-5:]:
            print(
                f"  - {day.get('id', '?')}  "
                f"waga: {day.get('weight', '-')}  "
                f"HRV: {day.get('hrv', '-')}  "
                f"tętno spocz.: {day.get('restingHR', '-')}  "
                f"kroki: {day.get('steps', '-')}  "
                f"sen (s): {day.get('sleepSecs', '-')}"
            )

    print(
        "\n✅ Jeśli oba wywołania zwróciły HTTP 200 - klucz działa i dane "
        "z Suunto realnie tam są. Jeśli wellness jest puste, to znaczy że "
        "Intervals.icu nie dostaje snu/HRV z Suunto automatycznie (do "
        "sprawdzenia w ustawieniach synchronizacji na intervals.icu)."
    )


if __name__ == "__main__":
    main()
