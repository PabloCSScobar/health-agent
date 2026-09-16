"""Długoterminowa pamięć per agent - kilka zdań faktów, nie cała historia.

Patrz sekcja 3 planu: "Specjaliści są bezstanowi między pytaniami; stan to
baza + agent_memory". Dzięki temu kontekst nie puchnie, a agent i tak
"pamięta" wnioski z poprzednich rozmów.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import AgentMemory
from health_agent.db.session import get_session


def remember(agent: str, key: str, value: str) -> None:
    with get_session() as session:
        stmt = (
            pg_insert(AgentMemory)
            .values(agent=agent, key=key, value=value, updated_at=dt.datetime.now(dt.timezone.utc))
            .on_conflict_do_update(index_elements=["agent", "key"], set_={"value": value, "updated_at": dt.datetime.now(dt.timezone.utc)})
        )
        session.execute(stmt)


def recall_all(agent: str) -> dict[str, str]:
    with get_session() as session:
        rows = session.execute(select(AgentMemory).where(AgentMemory.agent == agent)).scalars().all()
        return {r.key: r.value for r in rows}
