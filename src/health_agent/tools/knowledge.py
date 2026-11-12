"""Wiedza agentów (tabela `knowledge`) i dokumenty źródłowe (`documents`).

Trzy warstwy (patrz TODO.md, "Import wiedzy"):
- dokument = oryginał w całości (nic nie ginie),
- wiedza = fakty z datą, źródłem, rodzajem, flagą aktywny + rekoncyliacja
  (stary fakt nie znika, wskazuje `superseded_by` na nowszy),
- digest = to, co idzie do promptu specjalisty: aktywne, najważniejsze,
  z datami, z limitem znaków - reszta przez narzędzia get_knowledge /
  read_document.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select

from health_agent.db.models import Document, Knowledge
from health_agent.db.session import get_session

DOMAINS = ("running", "strength", "nutrition", "body", "recovery", "general")
KINDS = ("fakt", "zdarzenie", "zyciowka", "preferencja", "lekcja", "decyzja", "wniosek")
# Kolejność w digeście: to, co najbardziej zmienia rekomendacje, najpierw.
_KIND_PRIORITY = {k: i for i, k in enumerate(("lekcja", "decyzja", "preferencja", "zyciowka", "fakt", "zdarzenie", "wniosek"))}
DIGEST_LIMIT_CHARS = 1500


def _row(k: Knowledge) -> dict:
    return {
        "id": k.id, "domain": k.domain, "kind": k.kind, "content": k.content,
        "event_date": k.event_date.isoformat() if k.event_date else None,
        "source": f"{k.source_type}:{k.source_id}" if k.source_id else k.source_type,
        "confidence": k.confidence, "active": k.active, "superseded_by": k.superseded_by,
    }


def add_knowledge(
    domain: str, kind: str, content: str, *, event_date: dt.date | None = None,
    source_type: str = "chat", source_id: int | None = None, source_agent: str | None = None,
    confidence: str = "medium",
) -> int:
    if domain not in DOMAINS:
        domain = "general"
    if kind not in KINDS:
        kind = "fakt"
    with get_session() as session:
        k = Knowledge(
            domain=domain, kind=kind, content=content.strip(), event_date=event_date,
            source_type=source_type, source_id=source_id, source_agent=source_agent, confidence=confidence,
        )
        session.add(k)
        session.flush()
        return k.id


def supersede_knowledge(old_id: int, new_id: int) -> None:
    with get_session() as session:
        old = session.get(Knowledge, old_id)
        if old:
            old.active = False
            old.superseded_by = new_id


def set_knowledge_active(ids: list[int], active: bool) -> list[int]:
    changed = []
    with get_session() as session:
        for i in ids:
            k = session.get(Knowledge, i)
            if k and k.active != active:
                k.active = active
                if active:
                    k.superseded_by = None
                changed.append(i)
    return changed


def forget_knowledge(ids: list[int]) -> str:
    """Dezaktywuj wpisy wiedzy o podanych numerach (#id z listy po imporcie
    albo z get_knowledge) - gdy użytkownik mówi 'usuń 3 i 7', 'to
    nieprawda', 'zapomnij o kontuzji kolana'. Odwracalne (restore_knowledge)."""
    changed = set_knowledge_active(ids, False)
    return f"Usunięto z aktywnej wiedzy: {changed}" if changed else "Nic nie zmieniono (nieznane id albo już nieaktywne)."


def restore_knowledge(ids: list[int]) -> str:
    """Przywróć wcześniej usunięte/zastąpione wpisy wiedzy ('przywróć 12',
    'cofnij')."""
    changed = set_knowledge_active(ids, True)
    return f"Przywrócono: {changed}" if changed else "Nic nie zmieniono."


def get_knowledge(domain: str | None = None, kind: str | None = None, query: str | None = None, include_inactive: bool = False, limit: int = 30) -> list[dict]:
    """Wiedza o użytkowniku spoza danych pomiarowych: fakty z jego notatek/
    streszczeń, wnioski agentów, ustalenia z czatu. W prompcie masz tylko
    DIGEST (najważniejsze, z limitem) - tu jest reszta. `domain`: running |
    strength | nutrition | body | recovery | general (None = wszystkie);
    `kind`: fakt | zdarzenie | zyciowka | preferencja | lekcja | decyzja |
    wniosek; `query`: fragment treści (np. 'kolano', '10 km'). Każdy wpis ma
    `event_date` - oceniaj aktualność sam (życiówka z 2024 to punkt
    odniesienia, nie obecna forma) i `source` (document:ID -> pełny tekst
    przez read_document)."""
    with get_session() as session:
        stmt = select(Knowledge).order_by(Knowledge.event_date.desc().nullslast(), Knowledge.created_at.desc()).limit(limit)
        if not include_inactive:
            stmt = stmt.where(Knowledge.active.is_(True))
        if domain:
            stmt = stmt.where(Knowledge.domain.in_([domain, "general"]) if domain != "general" else Knowledge.domain == "general")
        if kind:
            stmt = stmt.where(Knowledge.kind == kind)
        if query:
            stmt = stmt.where(Knowledge.content.ilike(f"%{query}%"))
        return [_row(k) for k in session.execute(stmt).scalars().all()]


def knowledge_digest(domain: str, limit_chars: int = DIGEST_LIMIT_CHARS) -> str:
    """Blok do system promptu: aktywna wiedza domeny + general, posortowana
    po wadze rodzaju, potem po dacie, ucięta do limitu (z informacją, ile
    zostało poza digestem - żeby agent wiedział, że warto sięgnąć narzędziem)."""
    with get_session() as session:
        rows = session.execute(
            select(Knowledge).where(Knowledge.active.is_(True), Knowledge.domain.in_([domain, "general"]))
        ).scalars().all()
        items = [(k.kind, k.event_date, k.content, k.domain) for k in rows]
    if not items:
        return ""
    items.sort(key=lambda x: (_KIND_PRIORITY.get(x[0], 9), -(x[1].toordinal() if x[1] else 0)))
    lines, used, skipped = [], 0, 0
    for kind, date, content, dom in items:
        line = f"- [{kind}{', ' + date.isoformat() if date else ''}{', ' + dom if dom != domain else ''}] {content}"
        if used + len(line) > limit_chars:
            skipped += 1
            continue
        lines.append(line)
        used += len(line) + 1
    tail = f"\n(+{skipped} wpisów poza digestem - get_knowledge)" if skipped else ""
    return "\n\nWIEDZA O UŻYTKOWNIKU (z jego notatek, wcześniejszych ustaleń i Twoich wniosków; data = kiedy to było, oceniaj aktualność):\n" + "\n".join(lines) + tail


# --- dokumenty -------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def find_document_by_hash(sha: str) -> Document | None:
    with get_session() as session:
        d = session.execute(select(Document).where(Document.sha256 == sha)).scalar_one_or_none()
        if d:
            session.expunge(d)
        return d


def list_documents() -> list[dict]:
    """Zaimportowane dokumenty użytkownika (streszczenia rozmów, notatki):
    id, tytuł, data, domeny, krótkie streszczenie. Pełny tekst: read_document(id)."""
    with get_session() as session:
        rows = session.execute(select(Document).order_by(Document.imported_at.desc())).scalars().all()
        return [{"id": d.id, "title": d.title, "doc_date": d.doc_date.isoformat() if d.doc_date else None, "domains": d.domains, "summary": d.summary, "chars": len(d.text)} for d in rows]


def read_document(document_id: int) -> dict | None:
    """Pełny tekst zaimportowanego dokumentu (oryginał, bez skrótów). Użyj,
    gdy wpis wiedzy wskazuje `source: document:ID` i potrzebujesz kontekstu,
    albo gdy pytanie dotyczy czegoś, czego nie ma w digeście ani w
    get_knowledge - dokumenty są krótkie, czytaj w całości."""
    with get_session() as session:
        d = session.get(Document, document_id)
        if d is None:
            return None
        return {"id": d.id, "title": d.title, "doc_date": d.doc_date.isoformat() if d.doc_date else None, "domains": d.domains, "text": d.text}


def knowledge_from_document(document_id: int, include_inactive: bool = True) -> list[dict]:
    with get_session() as session:
        stmt = select(Knowledge).where(Knowledge.source_type == "document", Knowledge.source_id == document_id).order_by(Knowledge.id)
        if not include_inactive:
            stmt = stmt.where(Knowledge.active.is_(True))
        return [_row(k) for k in session.execute(stmt).scalars().all()]


SAVE_DOCUMENT_MIN_CHARS = 40


def save_document(
    text: str, *, title: str | None = None, doc_date: dt.date | None = None, source: str = "openbot"
) -> dict:
    """Zapisz oryginał notatki w `documents` BEZ wywołania LLM (bez ekstrakcji
    faktów). Deduplikacja po sha256 tekstu - ten sam tekst nie zapisze się
    drugi raz. Fakty z notatki dopisuje się osobno (remember_fact)."""
    from sqlalchemy.exc import IntegrityError

    text = text.strip()
    if len(text) < SAVE_DOCUMENT_MIN_CHARS:
        raise ValueError(f"Za krótki tekst na notatkę (min. {SAVE_DOCUMENT_MIN_CHARS} znaków).")
    sha = sha256_text(text)
    dup = find_document_by_hash(sha)
    if dup is not None:
        return {"id": dup.id, "title": dup.title, "deduplicated": True}
    final_title = ((title or "").strip() or f"notatka {dt.date.today().isoformat()}")[:256]
    try:
        with get_session() as session:
            doc = Document(
                title=final_title, source=source[:32], doc_date=doc_date,
                text=text, summary=None, domains=None, sha256=sha,
            )
            session.add(doc)
            session.flush()
            doc_id = doc.id
    except IntegrityError:
        dup = find_document_by_hash(sha)
        if dup is None:
            raise
        return {"id": dup.id, "title": dup.title, "deduplicated": True}
    return {"id": doc_id, "title": final_title, "deduplicated": False}
