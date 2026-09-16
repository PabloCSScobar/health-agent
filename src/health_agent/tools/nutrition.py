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
