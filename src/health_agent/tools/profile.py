"""Profil użytkownika - fakty podane WPROST przez użytkownika (cele, wzrost,
wiek, kontuzje, problemy zdrowotne, preferencje). Wstrzykiwany do system
promptu KAŻDEGO specjalisty (patrz registry._prompt_with_memory), więc coach
liczy białko na kg, porównuje z celem i omija np. refluks bez pytania.

Rozdział odpowiedzialności: profil = to, co powiedział użytkownik;
`agent_memory` per agent = wnioski agentów. Technicznie profil żyje w tej
samej tabeli `agent_memory` pod pseudo-agentem "user_profile" - świadomie,
żeby nie robić migracji dla key-value; jeśli kiedyś profil urośnie do
struktur (historia kontuzji z datami), wtedy własna tabela.
"""

from __future__ import annotations

from health_agent.tools.memory import recall_all, remember

PROFILE_AGENT = "user_profile"

PROFILE_KEYS: dict[str, str] = {
    "wzrost_cm": "wzrost w cm, liczba",
    "wiek": "wiek w latach, liczba",
    "plec": "M albo K (do BMR)",
    "cel_waga_kg": "docelowa waga, liczba",
    "cel_kcal_dzien": "dzienny cel kalorii, liczba",
    "cel_bialko_g_dzien": "dzienny cel białka w g, liczba",
    "cel_biegowy": "np. 'półmaraton 15.03.2027 poniżej 2h' albo 'budowa bazy'",
    "staz_treningowy": "od kiedy biega / ćwiczy siłowo",
    "kontuzje": "aktualne i przebyte, z datami jeśli znane",
    "problemy_zdrowotne": "np. refluks, nadciśnienie, alergie",
    "preferencje_zywieniowe": "co je / czego nie je / dieta",
    "suplementy": "co bierze i kiedy",
    "inne": "cokolwiek innego istotnego dla treningu/diety",
}


def get_user_profile() -> dict[str, str]:
    """Wszystkie fakty z profilu użytkownika (cele, wzrost, wiek, kontuzje...).
    Pusty słownik = użytkownik jeszcze nic nie podał."""
    return recall_all(PROFILE_AGENT)


def set_user_profile_facts(facts: dict[str, str]) -> str:
    """Zapisz/nadpisz fakty w profilu użytkownika - WSZYSTKIE z jednej
    wiadomości w JEDNYM wywołaniu (słownik klucz -> wartość), gdy użytkownik
    PODAJE coś o sobie (nie pyta): 'mam 34 lata i 182 cm' ->
    {"wiek": "34", "wzrost_cm": "182"}; 'mam refluks, chcę ważyć 84' ->
    {"problemy_zdrowotne": "refluks", "cel_waga_kg": "84"}.
    Klucze: wzrost_cm, wiek, plec (M/K), cel_waga_kg, cel_kcal_dzien,
    cel_bialko_g_dzien, cel_biegowy, staz_treningowy, kontuzje,
    problemy_zdrowotne, preferencje_zywieniowe, suplementy, inne.
    Liczby jako liczby ('84', nie 'około 84 kg'). Dla kontuzji/problemów/
    suplementów DOPISZ do istniejącej wartości (odczytaj get_user_profile),
    chyba że użytkownik prosi o usunięcie. Zwraca potwierdzenie - przekaż je
    użytkownikowi jednym zdaniem."""
    saved = []
    for key, value in facts.items():
        key = key.strip().lower().replace(" ", "_")
        if key not in PROFILE_KEYS:
            key = "inne"
        remember(PROFILE_AGENT, key, str(value).strip())
        saved.append(f"{key} = {str(value).strip()}")
    return "✅ Zapisano w profilu: " + "; ".join(saved)


def profile_prompt_block() -> str:
    facts = get_user_profile()
    if not facts:
        return (
            "\n\nPROFIL UŻYTKOWNIKA: pusty - nie znasz jego wzrostu, wieku, celów ani "
            "ograniczeń zdrowotnych. Patrz zasada 5: jedno pytanie '❓' na końcu, "
            "gdy brak zmienia rekomendację."
        )
    lines = "\n".join(f"- {k}: {v}" for k, v in sorted(facts.items()))
    return f"\n\nPROFIL UŻYTKOWNIKA (fakty podane przez niego - uwzględniaj ZAWSZE):\n{lines}"


def _profile_number(key: str) -> float | None:
    v = get_user_profile().get(key)
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


# Fakty, o które warto zapytać z góry (wywiad) - wynikają z tego, czego
# używają narzędzia: BMR (wzrost/wiek/płeć), cele (body/nutrition/running),
# ograniczenia (plany treningowe, dieta). Reszta kluczy (suplementy, staż,
# inne) zbiera się w rozmowie.
ONBOARDING_QUESTIONS: dict[str, str] = {
    "wzrost_cm": "wzrost (cm)",
    "wiek": "wiek",
    "plec": "płeć (M/K)",
    "cel_waga_kg": "docelowa waga (kg) - albo 'bez celu'",
    "cel_biegowy": "cel biegowy - np. 'półmaraton 15.03.2027 poniżej 2h' albo 'budowa bazy, bez startów'",
    "kontuzje": "kontuzje aktualne/przebyte - albo 'brak'",
    "problemy_zdrowotne": "problemy zdrowotne istotne dla treningu/diety (np. refluks, nadciśnienie, alergie) - albo 'brak'",
    "preferencje_zywieniowe": "preferencje/wykluczenia żywieniowe - albo 'brak'",
}


def missing_onboarding_keys() -> list[str]:
    have = get_user_profile()
    return [k for k in ONBOARDING_QUESTIONS if k not in have]


def onboarding_message() -> str | None:
    """Jedna wiadomość z pytaniami o brakujące fakty profilu (None = komplet).
    Użytkownik odpowiada swobodnie jedną wiadomością - orchestrator parsuje
    odpowiedź do set_user_profile_facts (ZASADA 3 w jego prompcie)."""
    missing = missing_onboarding_keys()
    if not missing:
        return None
    lines = [f"{i}. {ONBOARDING_QUESTIONS[k]}" for i, k in enumerate(missing, 1)]
    return (
        "Żeby analizy były o Tobie, a nie ogólne, brakuje mi kilku rzeczy. "
        "Odpowiedz jedną wiadomością, dowolnie (np. \"182 cm, 34 lata, M, cel 84 kg, ...\"):\n"
        + "\n".join(lines)
    )
