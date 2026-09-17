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
            "ograniczeń zdrowotnych. Gdy to ważne dla odpowiedzi, powiedz jednym "
            "zdaniem, co warto podać (np. 'podaj wzrost i wiek, policzę BMR')."
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
