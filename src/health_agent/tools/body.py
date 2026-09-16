"""Narzędzia odczytu wagi/składu ciała - wołane przez BodyCompCoach."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select

from health_agent.db.models import BodyComposition
from health_agent.db.session import get_session


class BodyCompositionPoint(BaseModel):
    measured_at: dt.datetime
    source: str
    weight_kg: float | None
    fat_pct: float | None
    muscle_kg: float | None
    bone_kg: float | None


def get_body_composition_latest() -> BodyCompositionPoint | None:
    with get_session() as session:
        row = session.execute(
            select(BodyComposition).order_by(BodyComposition.measured_at.desc()).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return BodyCompositionPoint(
            measured_at=row.measured_at, source=row.source, weight_kg=row.weight_kg,
            fat_pct=row.fat_pct, muscle_kg=row.muscle_kg, bone_kg=row.bone_kg,
        )


def get_body_composition_trend(days: int = 30) -> list[BodyCompositionPoint]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with get_session() as session:
        rows = session.execute(
            select(BodyComposition).where(BodyComposition.measured_at >= since).order_by(BodyComposition.measured_at)
        ).scalars().all()
        return [
            BodyCompositionPoint(
                measured_at=r.measured_at, source=r.source, weight_kg=r.weight_kg,
                fat_pct=r.fat_pct, muscle_kg=r.muscle_kg, bone_kg=r.bone_kg,
            )
            for r in rows
        ]
