"""
Smoke test: nieoficjalna biblioteka fitatu-api (reverse-engineered,
https://github.com/Jezue/fitatu_library) - sprawdza, czy w ogóle da się
pobrać dzisiejszy dziennik żywieniowy z Twojego konta Fitatu.

To jest największy element ryzyka w całym planie (endpointy mogą się
zmienić bez ostrzeżenia), więc traktujemy to jako pierwszy test.

Jak zdobyć dane logowania (bearer_token / refresh_token / fitatu_user_id):
    1. Otwórz https://www.fitatu.com/diary (lub app.fitatu.com) w przeglądarce, zaloguj się.
    2. F12 -> Application/Storage -> Local Storage -> znajdź klucze zawierające
       token (np. "token", "bearerToken") i dane usera (np. "user" z polem "id").
       Ewentualnie zakładka Network -> dowolne zapytanie do api.fitatu.com ->
       nagłówek Authorization: Bearer <TU JEST TOKEN>.
    3. Wklej wartości do .env (patrz .env.example).

Użycie:
    uv run scripts/test_fitatu.py
"""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv
from fitatu_api import FitatuLibrary

load_dotenv()


def main() -> None:
    bearer_token = os.environ.get("FITATU_BEARER_TOKEN")
    refresh_token = os.environ.get("FITATU_REFRESH_TOKEN")
    user_id = os.environ.get("FITATU_USER_ID")

    if not bearer_token:
        print(
            "⚠️  Brak FITATU_BEARER_TOKEN w .env - patrz instrukcja w docstringu "
            "tego skryptu (DevTools -> Local Storage / Network)."
        )
        return

    # Wartości domyślne w bibliotece (app_os="WEB", app_version="4.13.1",
    # api_cluster="pl-pl0") są nieaktualne względem realnego requestu z
    # przeglądarki (app-os: FITATU-WEB, app-version: 4.9.1,
    # api-cluster: pl-pl<user_id>) - nadpisujemy je, żeby nasz klient
    # wyglądał identycznie jak prawdziwa sesja webowa.
    session_data = {
        "bearer_token": bearer_token,
        "refresh_token": refresh_token,
        "fitatu_user_id": user_id,
        "app_os": os.environ.get("FITATU_APP_OS", "FITATU-WEB"),
        "app_version": os.environ.get("FITATU_APP_VERSION", "4.9.1"),
        "api_cluster": os.environ.get("FITATU_API_CLUSTER") or (f"pl-pl{user_id}" if user_id else None),
        "user_agent": os.environ.get(
            "FITATU_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        ),
    }

    lib = FitatuLibrary(session_data=session_data)

    print("=== Snapshot sesji (bez sekretów) ===")
    print(lib.describe_session())

    today = date.today()
    print(f"\n=== Podsumowanie dnia {today.isoformat()} ===")
    result = lib.get_day_summary_via_api(target_date=today)
    print(result)

    if result.get("status") == "ok":
        totals = result["result"].get("totals", {})
        print("\n✅ Sukces. Podsumowanie makro dzisiaj:")
        for key, value in totals.items():
            print(f"  {key}: {value}")
    else:
        print("\n❌ Błąd - sprawdź czy token nie wygasł (patrz FITATU_REFRESH_TOKEN) "
              "albo czy biblioteka nie wymaga aktualizacji (endpointy Fitatu mogły się zmienić).")


if __name__ == "__main__":
    main()
