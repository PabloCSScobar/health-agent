"""Import wiedzy z zewnątrz (streszczenia rozmów, notatki) - REKONCYLIACJA,
nie zrzut: importer dostaje istniejącą wiedzę i profil, zwraca operacje
add / update(id) / deactivate(id) z pytaniem przy konflikcie. Oryginał
dokumentu zostaje w całości w `documents` (read_document dla agentów).

Zasady rozstrzygania (patrz TODO.md "Import wiedzy"):
- wiedza: nowszy fakt z DATĄ późniejszą niż stary -> zastępuje (stary
  nieaktywny, superseded_by); brak dat albo remis -> oba aktywne + pytanie;
- profil: dopisuje brakujące klucze; istniejącą wartość nadpisuje TYLKO gdy
  dokument jest datowany później niż ostatni zapis tego klucza (nowsza
  informacja wygrywa, jak w wiedzy); niedatowany/starszy dokument -> pytanie
  (użytkownik rozstrzyga zwykłą wiadomością);
- aktywacja od razu, z cofnięciem ('usuń 3 i 7' / 'przywróć 12' /
  undo_import) - kolejka "do zatwierdzenia" to tarcie, którego nikt nie klika.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from health_agent.agents.base import build_agent, run_agent
from health_agent.db.models import Document, Knowledge
from health_agent.db.session import get_session
from health_agent.tools.knowledge import (
    DOMAINS,
    KINDS,
    add_knowledge,
    find_document_by_hash,
    get_knowledge,
    knowledge_from_document,
    set_knowledge_active,
    sha256_text,
    supersede_knowledge,
)
from health_agent.tools.profile import PROFILE_KEYS, get_user_profile, get_user_profile_updated_at, set_user_profile_facts

Domain = Literal["running", "strength", "nutrition", "body", "recovery", "general"]
Kind = Literal["fakt", "zdarzenie", "zyciowka", "preferencja", "lekcja", "decyzja", "wniosek"]


class KnowledgeOp(BaseModel):
    op: Literal["add", "update", "deactivate"]
    target_id: int | None = Field(default=None, description="id istniejącego wpisu dla update/deactivate")
    domain: Domain = "general"
    kind: Kind = "fakt"
    content: str = ""
    event_date: dt.date | None = None
    confidence: Literal["low", "medium", "high"] = "medium"
    conflict_question: str | None = Field(default=None, description="gdy nowy fakt jest sprzeczny z istniejącym i NIE wiadomo, który aktualny")


class ProfileOp(BaseModel):
    key: str
    value: str


class ImportExtraction(BaseModel):
    title: str
    summary: str = Field(description="2-3 zdania, o czym jest dokument")
    doc_date: dt.date | None = Field(default=None, description="data, której dotyczy treść (nie importu), jeśli da się ustalić")
    domains: list[Domain]
    knowledge: list[KnowledgeOp]
    profile: list[ProfileOp]


IMPORTER_PROMPT = f"""Jesteś importerem wiedzy o JEDNYM użytkowniku (biegacz-amator, trenuje siłowo, pilnuje diety) do jego osobistego systemu coachingowego. Dostajesz dokument (najczęściej streszczenie jego wcześniejszej rozmowy z AI albo notatkę) oraz ISTNIEJĄCĄ wiedzę i profil. Zwracasz operacje - to rekoncyliacja, nie przepisywanie.

CO WYCIĄGAĆ (tylko o użytkowniku, nie ogólne porady ani wiedza trenerska):
- fakt: stan/okoliczność (np. "biega od 2023", "trenuje rano, 5:30"),
- zdarzenie: coś, co się stało, Z DATĄ (kontuzja, choroba, przerwa, zmiana butów, start w zawodach),
- zyciowka: rekordy/wyniki z datą (10 km 52:10, 2024),
- preferencja: co lubi/czego nie (nie znosi interwałów na bieżni, źle znosi post),
- lekcja: wnioski, które się sprawdziły ("po <6h snu biegi są słabe", "za dużo błonnika = problemy żołądkowe"),
- decyzja: ustalenia/plany ("od października 3 biegi/tydz.", "cel: półmaraton w marcu").
Każdy wpis: jedno zdanie, konkretne, po polsku, z liczbami jeśli są. Data zdarzenia w `event_date` gdy da się ustalić (choćby z dokładnością do miesiąca - wtedy 1. dzień miesiąca). Spekulacje AI z rozmowy ("prawdopodobnie przetrenowanie") -> confidence "low" i sformułuj jako hipotezę, albo pomiń.

REKONCYLIACJA z istniejącą wiedzą (lista niżej, z id) - PRZED każdym wpisem sprawdź listę:
- ten sam fakt już jest -> NIE dodawaj (nawet innymi słowami),
- ten sam TEMAT z inną wartością (cel wagi 85 -> 84, cel czasowy 2:00 -> 2:05, nowy plan tygodnia, nowa liczba biegów) -> ZAWSZE op "update" z target_id starego wpisu, NIGDY "add" (dwa aktywne cele wagi to błąd); jeśli dokument jest nowszy -> zwykły update; jeśli nie wiadomo, który aktualny -> update + conflict_question,
- dokument mówi, że coś już nie obowiązuje (kontuzja wyleczona, plan porzucony, suplement odstawiony) -> op "deactivate" z target_id (content = jednozdaniowe uzasadnienie),
- inaczej -> "add".
Przykład: istnieje "14 | body | decyzja | 2026-06-01 | Cel wagowy: 85 kg do końca 2026", dokument mówi "celuję teraz w 84 kg" -> {{op: "update", target_id: 14, domain: "body", kind: "decyzja", content: "Cel wagowy: 84 kg do końca 2026 (zmienione z 85 kg)"}}.

PROFIL (klucze: {', '.join(PROFILE_KEYS)}): fakty pasujące do klucza dawaj TAKŻE w `profile` (wzrost, wiek, płeć, cele, kontuzje, problemy zdrowotne, preferencje żywieniowe, suplementy). Wartości zwięzłe, liczby jako liczby. System sam nie nadpisze istniejących wartości - tylko dopisze brakujące i zgłosi różnice.

Domeny: {', '.join(DOMAINS)} (general = nie pasuje do jednej). Rodzaje: {', '.join(KINDS)}.
Zwróć WYŁĄCZNIE strukturę. Lepiej 8 dobrych wpisów niż 25 rozwodnionych."""


def _existing_context() -> str:
    known = get_knowledge(include_inactive=False, limit=200)
    profile = get_user_profile()
    lines = ["ISTNIEJĄCA WIEDZA (id | domena | rodzaj | data | treść):"]
    lines += [f"{k['id']} | {k['domain']} | {k['kind']} | {k['event_date'] or '-'} | {k['content']}" for k in known] or ["(pusto)"]
    lines.append("\nPROFIL:")
    lines += [f"{k}: {v}" for k, v in sorted(profile.items())] or ["(pusty)"]
    return "\n".join(lines)


async def extract(text: str, title_hint: str | None) -> ImportExtraction:
    # retries=3: strukturalne wyjście z ~15 polami - model czasem zwróci coś
    # niezgodnego ze schematem (złapane w evalu), pydantic-ai wtedy odsyła
    # błąd walidacji i prosi o poprawkę zamiast wywalać cały import.
    agent = build_agent("importer", IMPORTER_PROMPT, [], output_type=ImportExtraction, retries=3)
    prompt = f"{_existing_context()}\n\nDOKUMENT{f' (tytuł pliku: {title_hint})' if title_hint else ''}:\n\"\"\"\n{text.strip()}\n\"\"\""
    return await run_agent(agent, "importer", prompt)


def _apply(doc: Document, ex: ImportExtraction) -> dict:
    """Zastosuj operacje; zwraca raport do pokazania użytkownikowi."""
    added, replaced, deactivated, conflicts, skipped = [], [], [], [], []
    with get_session() as session:
        existing = {k.id: k for k in session.execute(select(Knowledge)).scalars().all()}
        session.expunge_all()

    for op in ex.knowledge:
        if op.op == "add":
            if not op.content:
                continue
            new_id = add_knowledge(op.domain, op.kind, op.content, event_date=op.event_date, source_type="document", source_id=doc.id, confidence=op.confidence)
            added.append((new_id, op))
        elif op.op == "update":
            old = existing.get(op.target_id or -1)
            if old is None or not op.content:
                if op.content:
                    new_id = add_knowledge(op.domain, op.kind, op.content, event_date=op.event_date, source_type="document", source_id=doc.id, confidence=op.confidence)
                    added.append((new_id, op))
                continue
            new_id = add_knowledge(op.domain, op.kind, op.content, event_date=op.event_date, source_type="document", source_id=doc.id, confidence=op.confidence)
            newer = (op.event_date or doc.doc_date) and (old.event_date is None or (op.event_date or doc.doc_date) > old.event_date)
            if op.conflict_question and not newer:
                conflicts.append((old.id, new_id, old.content, op.content, op.conflict_question))
            else:
                supersede_knowledge(old.id, new_id)
                replaced.append((old.id, new_id, old.content, op.content))
        elif op.op == "deactivate":
            old = existing.get(op.target_id or -1)
            if old and old.active:
                set_knowledge_active([old.id], False)
                deactivated.append((old.id, old.content, op.content))

    profile_now = get_user_profile()
    profile_dates = get_user_profile_updated_at()
    profile_added, profile_updated, profile_conflicts = {}, [], []
    for pop in ex.profile:
        key = pop.key.strip().lower().replace(" ", "_")
        if key not in PROFILE_KEYS:
            skipped.append(f"profil: nieznany klucz {pop.key}")
            continue
        if key in profile_now:
            if str(profile_now[key]).strip().lower() == str(pop.value).strip().lower():
                continue
            # Dokument DATOWANY później niż ostatni zapis tego klucza -> nowsza
            # informacja wygrywa (jak w wiedzy); niedatowany albo starszy ->
            # pytanie, nie nadpisujemy po cichu.
            saved_at = profile_dates.get(key)
            if doc.doc_date and saved_at and doc.doc_date > saved_at.date():
                profile_updated.append((key, profile_now[key], pop.value))
                profile_added[key] = pop.value
            else:
                profile_conflicts.append((key, profile_now[key], pop.value))
        else:
            profile_added[key] = pop.value
    if profile_added:
        set_user_profile_facts(profile_added)

    return {
        "added": added, "replaced": replaced, "deactivated": deactivated, "conflicts": conflicts,
        "profile_added": {k: v for k, v in profile_added.items() if k not in {u[0] for u in profile_updated}},
        "profile_updated": profile_updated, "profile_conflicts": profile_conflicts, "skipped": skipped,
    }


def _format_report(doc: Document, ex: ImportExtraction, rep: dict) -> str:
    lines = [f"📄 Zaimportowano „{doc.title}” (#{doc.id}{', ' + doc.doc_date.isoformat() if doc.doc_date else ''}) - {ex.summary}"]
    if rep["added"]:
        lines.append(f"\nNowa wiedza ({len(rep['added'])}):")
        for new_id, op in rep["added"]:
            date = f", {op.event_date.isoformat()}" if op.event_date else ""
            lines.append(f"#{new_id} [{op.domain}/{op.kind}{date}] {op.content}" + (" (niska pewność)" if op.confidence == "low" else ""))
    if rep["replaced"]:
        lines.append(f"\nZaktualizowane ({len(rep['replaced'])}) - stare zostają w historii:")
        for old_id, new_id, old_c, new_c in rep["replaced"]:
            lines.append(f"#{new_id} {new_c}  ← było (#{old_id}): {old_c}")
    if rep["deactivated"]:
        lines.append(f"\nOznaczone jako nieaktualne ({len(rep['deactivated'])}):")
        for old_id, old_c, why in rep["deactivated"]:
            lines.append(f"#{old_id} {old_c}" + (f"  ({why})" if why else ""))
    if rep["profile_added"]:
        lines.append("\nProfil - dopisane: " + "; ".join(f"{k} = {v}" for k, v in rep["profile_added"].items()))
    if rep["profile_updated"]:
        lines.append("\nProfil - zaktualizowane (dokument nowszy niż poprzedni wpis): " + "; ".join(f"{k}: {old} → {new}" for k, old, new in rep["profile_updated"]))
    if rep["conflicts"] or rep["profile_conflicts"]:
        lines.append("\n❓ Do rozstrzygnięcia (oba warianty zostają, dopóki nie odpowiesz):")
        for old_id, new_id, old_c, new_c, q in rep["conflicts"]:
            lines.append(f"- {q}  [#{old_id}: {old_c}  vs  #{new_id}: {new_c}]")
        for key, old_v, new_v in rep["profile_conflicts"]:
            lines.append(f"- profil {key}: masz „{old_v}”, w notatce „{new_v}” - które aktualne? (odpisz np. „{key}: {new_v}”)")
    if rep["skipped"]:
        lines.append("\nPominięte: " + "; ".join(rep["skipped"]))
    if not (rep["added"] or rep["replaced"] or rep["deactivated"] or rep["profile_added"] or rep["profile_updated"]):
        lines.append("\nNie znalazłem nowych faktów o Tobie (wszystko już było albo dokument nie zawiera faktów).")
    lines.append("\nPoprawki: „usuń 3 i 7”, „przywróć 12”, „cofnij import” - oryginał notatki jest zapisany w całości.")
    return "\n".join(lines)


async def import_document(text: str, *, title: str | None = None, source: str = "cli", doc_date: dt.date | None = None) -> str:
    """Pełny przepływ: dedup po hashu -> zapis oryginału -> ekstrakcja ->
    rekoncyliacja -> raport (tekst do pokazania użytkownikowi)."""
    text = text.strip()
    if len(text) < 40:
        return "Za krótki tekst na notatkę (min. 40 znaków)."
    sha = sha256_text(text)
    dup = find_document_by_hash(sha)
    if dup:
        n = len(knowledge_from_document(dup.id, include_inactive=False))
        return f"Ten dokument jest już zaimportowany jako #{dup.id} „{dup.title}” ({n} aktywnych faktów). Nic nie zmieniono."

    ex = await extract(text, title)
    with get_session() as session:
        doc = Document(
            title=(title or ex.title or "notatka")[:256], source=source, doc_date=doc_date or ex.doc_date,
            text=text, summary=ex.summary, domains=list(dict.fromkeys(ex.domains)), sha256=sha,
        )
        session.add(doc)
        session.flush()
        session.expunge(doc)
    rep = _apply(doc, ex)
    return _format_report(doc, ex, rep)


def undo_import(document_id: int) -> str:
    """Pełne cofnięcie importu: usuwa wiedzę z tego dokumentu, przywraca
    wpisy, które ten import zastąpił lub dezaktywował, usuwa dokument (żeby
    ten sam plik dało się zaimportować ponownie - hash by go zablokował).
    Zmian w PROFILU nie cofa (są w jednym słowniku bez historii) - raport
    importu wypisał, co zmienił, użytkownik poprawia jednym zdaniem."""
    with get_session() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            return f"Nie ma dokumentu #{document_id}."
        rows = session.execute(select(Knowledge).where(Knowledge.source_type == "document", Knowledge.source_id == document_id)).scalars().all()
        ids = [r.id for r in rows]
        restored = []
        if ids:
            for old in session.execute(select(Knowledge).where(Knowledge.superseded_by.in_(ids))).scalars().all():
                old.active, old.superseded_by = True, None
                restored.append(old.id)
        for r in rows:
            session.delete(r)
        # dezaktywowane przez ten import (op "deactivate") nie mają wskaźnika -
        # przywracamy wpisy nieaktywne bez superseded_by, wyłączone po imporcie
        for old in session.execute(select(Knowledge).where(Knowledge.active.is_(False), Knowledge.superseded_by.is_(None), Knowledge.updated_at >= doc.imported_at)).scalars().all():
            old.active = True
            restored.append(old.id)
        title = doc.title
        session.delete(doc)
    return f"Cofnięto import #{document_id} „{title}”: usunięto {len(ids)} wpisów wiedzy, przywrócono {len(restored)} wcześniejszych. Profil bez zmian - popraw ręcznie, jeśli import go zmienił."
