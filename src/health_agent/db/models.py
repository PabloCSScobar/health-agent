"""Model danych - patrz sekcja 5 planu w ~/.claude/plans/... po uzasadnienie.

Każdy ingestor działa wg wzorca: pobierz -> zapisz do `raw_payloads` (audyt,
replay) -> znormalizuj -> upsert po kluczu naturalnym (`source` + `external_id`
albo `source` + data). Dzięki temu ponowne uruchomienie ingestora jest zawsze
bezpieczne (idempotentne), nawet jeśli to samo źródło przyśle te same dane
drugi raz (np. Health Connect Webhook ma lookback 48h i może wysłać tę samą
pozycję kilka razy, zanim wypadnie z okna).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RawPayload(Base):
    """Wszystko co przyszło z zewnątrz, zanim je znormalizujemy - audyt/replay."""

    __tablename__ = "raw_payloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON)


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_workouts_source_external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # np. "intervals_icu", "suunto"
    external_id: Mapped[str] = mapped_column(String(128))
    sport: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[float | None] = mapped_column(Float)
    avg_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    calories: Mapped[float | None] = mapped_column(Float)
    ascent_m: Mapped[float | None] = mapped_column(Float)
    avg_pace: Mapped[float | None] = mapped_column(Float)  # sek/km, wg tego co da źródło
    training_effect_aerobic: Mapped[float | None] = mapped_column(Float)
    vo2max_est: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class DailyActivity(Base):
    __tablename__ = "daily_activity"
    __table_args__ = (UniqueConstraint("source", "date", name="uq_daily_activity_source_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    date: Mapped[dt.date] = mapped_column(index=True)
    steps: Mapped[int | None] = mapped_column(Integer)
    calories_active: Mapped[float | None] = mapped_column(Float)
    calories_total: Mapped[float | None] = mapped_column(Float)
    resting_hr: Mapped[int | None] = mapped_column(Integer)


class Sleep(Base):
    __tablename__ = "sleep"
    __table_args__ = (UniqueConstraint("source", "date", name="uq_sleep_source_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    date: Mapped[dt.date] = mapped_column(index=True)
    start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    deep_s: Mapped[int | None] = mapped_column(Integer)
    rem_s: Mapped[int | None] = mapped_column(Integer)
    light_s: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class Recovery(Base):
    __tablename__ = "recovery"
    __table_args__ = (UniqueConstraint("source", "date", name="uq_recovery_source_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    date: Mapped[dt.date] = mapped_column(index=True)
    hrv: Mapped[float | None] = mapped_column(Float)
    resources_pct: Mapped[float | None] = mapped_column(Float)
    stress: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class BodyComposition(Base):
    __tablename__ = "body_composition"
    __table_args__ = (UniqueConstraint("source", "measured_at", name="uq_body_comp_source_measured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # "fitdays_healthconnect", "manual"
    measured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    fat_pct: Mapped[float | None] = mapped_column(Float)
    muscle_kg: Mapped[float | None] = mapped_column(Float)  # lean_body_mass
    bone_kg: Mapped[float | None] = mapped_column(Float)
    water_pct: Mapped[float | None] = mapped_column(Float)
    bmi: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class NutritionDay(Base):
    __tablename__ = "nutrition_days"

    date: Mapped[dt.date] = mapped_column(primary_key=True)
    kcal: Mapped[float | None] = mapped_column(Float)
    protein_g: Mapped[float | None] = mapped_column(Float)
    fat_g: Mapped[float | None] = mapped_column(Float)
    carbs_g: Mapped[float | None] = mapped_column(Float)
    fiber_g: Mapped[float | None] = mapped_column(Float)
    sugar_g: Mapped[float | None] = mapped_column(Float)
    salt_g: Mapped[float | None] = mapped_column(Float)
    water_ml: Mapped[float | None] = mapped_column(Float)


class NutritionItem(Base):
    __tablename__ = "nutrition_items"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_nutrition_items_source_external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[dt.date] = mapped_column(ForeignKey("nutrition_days.date"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # "fitatu_healthconnect", "fitatu_api"
    # external_id: hash treści (nazwa+kalorie+makra+dzień), bo payloady z Health
    # Connect nie mają stabilnego ID pozycji (patrz komentarz w ingest/healthconnect.py)
    external_id: Mapped[str] = mapped_column(String(64))
    meal: Mapped[str | None] = mapped_column(String(64))
    product: Mapped[str | None] = mapped_column(String(256))
    qty: Mapped[float | None] = mapped_column(Float)
    kcal: Mapped[float | None] = mapped_column(Float)
    protein_g: Mapped[float | None] = mapped_column(Float)
    fat_g: Mapped[float | None] = mapped_column(Float)
    carbs_g: Mapped[float | None] = mapped_column(Float)
    sugar_g: Mapped[float | None] = mapped_column(Float)
    fiber_g: Mapped[float | None] = mapped_column(Float)
    salt_g: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class ManualLog(Base):
    __tablename__ = "manual_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    logged_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # strength | weight | note | wellbeing
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    text_original: Mapped[str | None] = mapped_column(String)


class Document(Base):
    """Zaimportowany dokument (streszczenie rozmowy, notatka) - ORYGINAŁ w
    całości. Fakty wyciągnięte z niego lądują w `knowledge` ze wskazaniem na
    ten wiersz; gdy ekstrakcja coś pominie, agent może wrócić do źródła
    przez read_document. `sha256` chroni przed podwójnym importem."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(32))  # cli | telegram_file | telegram_text
    doc_date: Mapped[dt.date | None] = mapped_column(Date)  # data, której dotyczy treść (nie importu)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    text: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(String)
    domains: Mapped[list | None] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)


class Knowledge(Base):
    """Wszystko, co agenci "wiedzą" poza danymi pomiarowymi: fakty z importu,
    wnioski agentów (remember_fact), fakty z czatu. Zastępuje agent_memory
    dla wiedzy (agent_memory zostaje tylko dla profilu użytkownika):
    - `domain` = nazwa specjalisty (running/...) albo "general",
    - `kind`: fakt | zdarzenie | zyciowka | preferencja | lekcja | decyzja | wniosek,
    - `event_date`: kiedy to było / od kiedy obowiązuje - agent sam ocenia,
      czy "PB 10 km z 2024" jest jeszcze aktualne,
    - `source_type`/`source_id`/`source_agent`: skąd (document/agent/chat),
    - `active` + `superseded_by`: rekoncyliacja zamiast nadpisywania - stary
      fakt zostaje w historii, wskazuje na nowszy.
    Do promptu idzie tylko DIGEST (patrz tools/knowledge.py), nie cała tabela."""

    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(String)
    event_date: Mapped[dt.date | None] = mapped_column(Date)
    source_type: Mapped[str] = mapped_column(String(16))  # document | agent | chat
    source_id: Mapped[int | None] = mapped_column(Integer)  # documents.id dla document
    source_agent: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[str] = mapped_column(String(8), default="medium")  # low | medium | high
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("knowledge.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class IngestState(Base):
    """Data ostatniego udanego pollu per źródło - żeby po przerwie (serwer
    wyłączony, awaria) polling sam dociągnął CAŁĄ lukę, a nie tylko stałe
    ostatnie N dni (patrz scheduler.py: poll_intervals_icu)."""

    __tablename__ = "ingest_state"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_synced_date: Mapped[dt.date] = mapped_column(Date)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AgentMemory(Base):
    """Od 2026-09-17 tylko profil użytkownika (agent="user_profile", patrz
    tools/profile.py). Wiedza agentów przeniesiona do `knowledge`."""

    __tablename__ = "agent_memory"
    __table_args__ = (UniqueConstraint("agent", "key", name="uq_agent_memory_agent_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(String)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_conversations_chat_message"),
    )
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(String)
    agent: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    telegram_message_ids: Mapped[list | None] = mapped_column(JSON)
    root_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    parent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    # Nazwy narzędzi wywołanych w tym biegu, w kolejności (bez argumentów) -
    # do eval_agents.py ("czy running użył analyze_run?") i debugowania.
    tools_called: Mapped[list | None] = mapped_column(JSON)


class Feedback(Base):
    """Ocena odpowiedzi wysłanej na Telegramie.

    Jeden wiersz na wiadomość asystenta; zmiana reakcji aktualizuje ocenę,
    zamiast dopisywać kolejną. ``agent_run_id`` jest opcjonalne dla
    deterministycznych odpowiedzi bez wywołania modelu.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_feedback_conversation"),
        CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
