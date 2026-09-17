"""Narzędzia odczytu danych o odżywianiu - wołane przez NutritionCoach."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select

from health_agent.db.models import NutritionDay, NutritionItem
from health_agent.db.session import get_session


class NutritionDaySummary(BaseModel):
    date: dt.date
    kcal: float | None
    protein_g: float | None
    fat_g: float | None
    carbs_g: float | None
    fiber_g: float | None
    sugar_g: float | None
    salt_g: float | None
    items: list[str]


def get_nutrition_day(date: dt.date) -> NutritionDaySummary | None:
    """Podsumowanie dnia: makra + lista nazw zjedzonych produktów."""
    with get_session() as session:
        day = session.get(NutritionDay, date)
        if day is None:
            return None
        items = session.execute(
            select(NutritionItem.product).where(NutritionItem.day == date)
        ).scalars().all()
        return NutritionDaySummary(
            date=day.date, kcal=day.kcal, protein_g=day.protein_g, fat_g=day.fat_g,
            carbs_g=day.carbs_g, fiber_g=day.fiber_g, sugar_g=day.sugar_g, salt_g=day.salt_g,
            items=[i for i in items if i],
        )


def get_nutrition_range(start: dt.date, end: dt.date) -> list[NutritionDaySummary]:
    """Podsumowania dzienne dla zakresu dat (bez listy produktów - do trendów)."""
    with get_session() as session:
        days = session.execute(
            select(NutritionDay).where(NutritionDay.date >= start, NutritionDay.date <= end).order_by(NutritionDay.date)
        ).scalars().all()
        return [
            NutritionDaySummary(
                date=d.date, kcal=d.kcal, protein_g=d.protein_g, fat_g=d.fat_g,
                carbs_g=d.carbs_g, fiber_g=d.fiber_g, sugar_g=d.sugar_g, salt_g=d.salt_g,
                items=[],
            )
            for d in days
        ]


# ---------------------------------------------------------------------------
# Analityka: średnie vs cele, białko na kg, bilans energetyczny, wyszukiwanie
# produktów - to, co dietetyk policzyłby sam.
# ---------------------------------------------------------------------------

import statistics  # noqa: E402

from sqlalchemy import func  # noqa: E402

from health_agent.tools.energy import _latest_weight, estimate_daily_expenditure  # noqa: E402
from health_agent.tools.profile import _profile_number  # noqa: E402

PROTEIN_G_PER_KG_RANGE = (1.6, 2.2)  # dla trenujących (bieganie + siła); Morton 2018 / ISSN


def get_nutrition_summary(days: int = 7) -> dict:
    """ZACZNIJ OD TEGO. Dni z ostatnich `days` z danymi (kcal, białko,
    tłuszcz, węgle, błonnik, liczba pozycji), średnie z dni Z DANYMI,
    porównanie z celami z profilu (kcal, białko) albo z zakresem białka
    1.6-2.2 g/kg wg ostatniej wagi, udział makro w kcal, weekend vs tydzień.
    UWAGA: dni bez wpisów to brak danych (użytkownik nie logował), nie 0
    kcal - `days_with_data` mówi, ile dni naprawdę masz; przy <5 dniach nie
    wyciągaj wniosków o "nawykach"."""
    since = dt.date.today() - dt.timedelta(days=days - 1)
    with get_session() as session:
        rows = session.execute(
            select(NutritionDay).where(NutritionDay.date >= since, NutritionDay.kcal.isnot(None)).order_by(NutritionDay.date)
        ).scalars().all()
        counts = dict(
            session.execute(
                select(NutritionItem.day, func.count()).where(NutritionItem.day >= since).group_by(NutritionItem.day)
            ).all()
        )
    day_rows = [
        {"date": d.date.isoformat(), "weekday": d.date.strftime("%a"), "kcal": round(d.kcal), "protein_g": round(d.protein_g or 0),
         "fat_g": round(d.fat_g or 0), "carbs_g": round(d.carbs_g or 0), "fiber_g": round(d.fiber_g or 0), "items": counts.get(d.date, 0)}
        for d in rows
    ]
    if not day_rows:
        return {"days_requested": days, "days_with_data": 0, "note": "brak danych o odżywianiu w tym okresie"}

    def avg(key):
        return round(statistics.mean(r[key] for r in day_rows))

    weight = _latest_weight(dt.date.today())
    kcal_goal, protein_goal = _profile_number("cel_kcal_dzien"), _profile_number("cel_bialko_g_dzien")
    protein_avg = avg("protein_g")
    kcal_avg = avg("kcal")
    weekend = [r for r in day_rows if r["weekday"] in ("Sat", "Sun")]
    weekday = [r for r in day_rows if r["weekday"] not in ("Sat", "Sun")]
    return {
        "days_requested": days,
        "days_with_data": len(day_rows),
        "days": day_rows,
        "avg": {"kcal": kcal_avg, "protein_g": protein_avg, "fat_g": avg("fat_g"), "carbs_g": avg("carbs_g"), "fiber_g": avg("fiber_g")},
        "macro_pct_of_kcal": {
            "protein": round(protein_avg * 4 / kcal_avg * 100) if kcal_avg else None,
            "fat": round(avg("fat_g") * 9 / kcal_avg * 100) if kcal_avg else None,
            "carbs": round(avg("carbs_g") * 4 / kcal_avg * 100) if kcal_avg else None,
        },
        "protein_g_per_kg": round(protein_avg / weight, 2) if weight else None,
        "protein_target": {
            "from_profile_g": protein_goal,
            "range_g_by_weight": [round(weight * PROTEIN_G_PER_KG_RANGE[0]), round(weight * PROTEIN_G_PER_KG_RANGE[1])] if weight else None,
            "weight_kg_used": weight,
        },
        "kcal_target_from_profile": kcal_goal,
        "kcal_vs_target": round(kcal_avg - kcal_goal) if kcal_goal else None,
        "weekend_vs_weekday_kcal": {"weekend_avg": round(statistics.mean(r["kcal"] for r in weekend)) if weekend else None, "weekday_avg": round(statistics.mean(r["kcal"] for r in weekday)) if weekday else None},
        "fiber_note": "cel ~25-38 g/dzień" ,
    }


def get_energy_balance(days: int = 7, end_date: dt.date | None = None) -> dict:
    """Bilans energetyczny dzień po dniu za `days` dni KOŃCZĄC na `end_date`
    (domyślnie dziś; "wczoraj" = end_date=wczorajsza data, days=1; "ten
    tydzień" = days=7): zjedzone (Fitatu) minus wydatek
    (wpis ręczny użytkownika ALBO szacunek BMR+treningi+kroki+TEF, patrz
    `method`), średnia, suma i przełożenie na kg (7700 kcal ≈ 1 kg tkanki
    tłuszczowej - orientacyjnie). To narzędzie odpowiada na "jaki mam
    deficyt" BEZ pytania recovery. Jeśli `missing` niepuste - szacunek
    jest niepełny, powiedz użytkownikowi, co uzupełnić w profilu."""
    end = end_date or dt.date.today()
    since = end - dt.timedelta(days=days - 1)
    with get_session() as session:
        intake = {d.date: d.kcal for d in session.execute(select(NutritionDay).where(NutritionDay.date >= since, NutritionDay.date <= end, NutritionDay.kcal.isnot(None))).scalars().all()}
    rows, missing = [], set()
    for i in range(days):
        day = since + dt.timedelta(days=i)
        if day not in intake:
            continue
        exp = estimate_daily_expenditure(day)
        missing.update(exp.get("missing", []))
        rows.append({
            "date": day.isoformat(), "intake_kcal": round(intake[day]), "expenditure_kcal": exp["total_kcal"], "method": exp["method"],
            "balance_kcal": round(intake[day] - exp["total_kcal"]) if exp["total_kcal"] else None,
        })
    balances = [r["balance_kcal"] for r in rows if r["balance_kcal"] is not None]
    total = sum(balances) if balances else None
    return {
        "period": {"from": since.isoformat(), "to": end.isoformat()},
        "days_with_data": len(rows),
        "days": rows,
        "avg_balance_kcal": round(statistics.mean(balances)) if balances else None,
        "sum_balance_kcal": total,
        "implied_kg_change": round(total / 7700, 2) if total is not None else None,
        "missing_for_estimate": sorted(missing),
        "note": "ujemny bilans = deficyt; wydatek 'estimate' ±10-15%; 'manual' = liczba podana przez użytkownika",
    }


def find_foods(query: str | None = None, days: int = 14, sort_by: str = "kcal") -> dict:
    """Pozycje z dziennika: `query` (fragment nazwy, np. 'kurczak') albo -
    bez query - TOP produkty z ostatnich `days` dni wg `sort_by` ('kcal' |
    'protein_g' | 'fat_g' | 'carbs_g' | 'sugar_g'), zagregowane po nazwie
    (ile razy, suma, średnia porcja). Do pytań "co jem najwięcej", "skąd
    mam tyle tłuszczu", "co jadłem bogatego w białko", "czy jadłem X"."""
    since = dt.date.today() - dt.timedelta(days=days - 1)
    col = {"kcal": NutritionItem.kcal, "protein_g": NutritionItem.protein_g, "fat_g": NutritionItem.fat_g, "carbs_g": NutritionItem.carbs_g, "sugar_g": NutritionItem.sugar_g}.get(sort_by, NutritionItem.kcal)
    with get_session() as session:
        stmt = (
            select(NutritionItem.product, func.count(), func.sum(NutritionItem.kcal), func.sum(NutritionItem.protein_g), func.sum(NutritionItem.fat_g), func.sum(NutritionItem.carbs_g), func.max(NutritionItem.day), func.sum(col))
            .where(NutritionItem.day >= since)
            .group_by(NutritionItem.product)
            .order_by(func.sum(col).desc())
            .limit(15)
        )
        if query:
            stmt = stmt.where(NutritionItem.product.ilike(f"%{query}%"))
        rows = session.execute(stmt).all()
    return {
        "days": days, "query": query, "sorted_by": sort_by,
        "items": [
            {"product": p, "times": n, "total_kcal": round(k or 0), "total_protein_g": round(pr or 0), "total_fat_g": round(f or 0), "total_carbs_g": round(c or 0), "avg_kcal_per_entry": round((k or 0) / n), "last": str(last)}
            for p, n, k, pr, f, c, last, _ in rows
        ],
    }
