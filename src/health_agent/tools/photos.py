"""Lokalne archiwum zdjęć sylwetki.

Ten moduł nie jest rejestrowany jako narzędzie agentów. Bajty zdjęć nigdy nie
trafiają do promptu ani zewnętrznego API LLM.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import io
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import BodyComposition, ProgressPhoto
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.time_utils import local_date

ALLOWED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
PHOTO_VIEWS = {"front", "side", "back", "other"}


def photo_root() -> Path:
    root = Path(settings.progress_photos_dir)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[3] / root
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def photo_archive_lock():
    """Blokada procesowa na współdzielonym wolumenie API/bota."""
    lock_path = photo_root() / ".archive.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validated_image(data: bytes) -> tuple[str, str, int, int]:
    if not data:
        raise ValueError("Pusty plik")
    if len(data) > settings.progress_photo_max_bytes:
        raise ValueError("Zdjęcie przekracza limit 15 MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Plik nie jest poprawnym obrazem JPEG/PNG/WebP") from exc
    if image_format not in ALLOWED_FORMATS:
        raise ValueError("Dozwolone formaty: JPEG, PNG i WebP")
    if width <= 0 or height <= 0 or width * height > settings.progress_photo_max_pixels:
        raise ValueError("Zdjęcie ma nieprawidłowe wymiary lub przekracza 25 MP")
    content_type, extension = ALLOWED_FORMATS[image_format]
    return content_type, extension, width, height


def _row(photo: ProgressPhoto) -> dict:
    return {
        "id": photo.id,
        "captured_date": photo.captured_date.isoformat(),
        "view": photo.view,
        "note": photo.note,
        "content_type": photo.content_type,
        "bytes_size": photo.bytes_size,
        "width": photo.width,
        "height": photo.height,
        "state": photo.state,
        "source": photo.source,
        "created_at": photo.created_at.isoformat(),
    }


def _save_progress_photo(
    data: bytes,
    *,
    captured_date: dt.date,
    view: str,
    note: str | None = None,
    source: str,
) -> dict:
    view = view.lower()
    if view not in PHOTO_VIEWS:
        raise ValueError(f"Nieznany widok zdjęcia: {view}")
    content_type, extension, width, height = _validated_image(data)
    digest = hashlib.sha256(data).hexdigest()
    storage_key = f"{captured_date.isoformat()}_{view}_{digest[:16]}{extension}"
    now = dt.datetime.now(dt.timezone.utc)
    values = {
        "captured_date": captured_date,
        "view": view,
        "note": note.strip() if note else None,
        "storage_key": storage_key,
        "content_type": content_type,
        "bytes_size": len(data),
        "width": width,
        "height": height,
        "sha256": digest,
        "state": "staging",
        "source": source,
        "created_at": now,
    }
    with get_session() as session:
        session.execute(
            pg_insert(ProgressPhoto)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_progress_photos_sha256")
        )
        photo = session.execute(
            select(ProgressPhoto).where(ProgressPhoto.sha256 == digest)
        ).scalar_one()
        photo_id = photo.id
        if photo.state == "ready":
            return {**_row(photo), "deduplicated": True}
        old_storage_key = photo.storage_key
        for key, value in values.items():
            setattr(photo, key, value)
        photo.deleted_at = None

    root = photo_root()
    final_path = root / storage_key
    old_path = root / Path(old_storage_key).name
    temporary = root / f".{storage_key}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final_path)
        if old_path != final_path:
            old_path.unlink(missing_ok=True)
        with get_session() as session:
            photo = session.get(ProgressPhoto, photo_id)
            if photo is None:
                raise RuntimeError("Brak metadanych zdjęcia po zapisie")
            photo.state = "ready"
            return {**_row(photo), "deduplicated": False}
    except Exception:
        temporary.unlink(missing_ok=True)
        if old_path != final_path:
            old_path.unlink(missing_ok=True)
        raise


def save_progress_photo(
    data: bytes,
    *,
    captured_date: dt.date,
    view: str,
    note: str | None = None,
    source: str,
) -> dict:
    with photo_archive_lock():
        return _save_progress_photo(
            data,
            captured_date=captured_date,
            view=view,
            note=note,
            source=source,
        )


def list_progress_photos(view: str | None = None, limit: int = 100) -> list[dict]:
    with get_session() as session:
        stmt = (
            select(ProgressPhoto)
            .where(ProgressPhoto.state == "ready")
            .order_by(ProgressPhoto.captured_date.desc(), ProgressPhoto.id.desc())
            .limit(min(max(limit, 1), 500))
        )
        if view:
            if view not in PHOTO_VIEWS:
                raise ValueError("Nieznany widok zdjęcia")
            stmt = stmt.where(ProgressPhoto.view == view)
        photos = session.execute(stmt).scalars().all()
        body = session.execute(
            select(BodyComposition).order_by(BodyComposition.measured_at)
        ).scalars().all()
    body_by_day = {}
    for measurement in body:
        body_by_day[local_date(measurement.measured_at)] = measurement
    result = []
    for photo in photos:
        measurement = body_by_day.get(photo.captured_date)
        result.append(
            {
                **_row(photo),
                "weight_kg": measurement.weight_kg if measurement else None,
                "fat_pct": measurement.fat_pct if measurement else None,
            }
        )
    return result


def progress_photo_path(photo_id: int) -> tuple[Path, str] | None:
    with get_session() as session:
        photo = session.get(ProgressPhoto, photo_id)
        if photo is None or photo.state != "ready":
            return None
        storage_key, content_type = photo.storage_key, photo.content_type
    path = photo_root() / Path(storage_key).name
    if not path.is_file():
        return None
    return path, content_type


def _delete_progress_photo(photo_id: int) -> bool:
    with get_session() as session:
        photo = session.get(ProgressPhoto, photo_id)
        if photo is None or photo.state == "deleted":
            return False
        photo.state = "deleting"
        storage_key = photo.storage_key
    (photo_root() / Path(storage_key).name).unlink(missing_ok=True)
    with get_session() as session:
        photo = session.get(ProgressPhoto, photo_id)
        if photo:
            photo.state = "deleted"
            photo.deleted_at = dt.datetime.now(dt.timezone.utc)
    return True


def delete_progress_photo(photo_id: int) -> bool:
    with photo_archive_lock():
        return _delete_progress_photo(photo_id)


def _reconcile_progress_photos() -> dict:
    root = photo_root()
    fixed = deleted = 0
    with get_session() as session:
        rows = session.execute(
            select(ProgressPhoto).where(ProgressPhoto.state.in_(["staging", "deleting"]))
        ).scalars().all()
        for photo in rows:
            path = root / Path(photo.storage_key).name
            if photo.state == "staging" and path.is_file():
                photo.state = "ready"
                fixed += 1
            elif photo.state == "staging":
                session.delete(photo)
                deleted += 1
            else:
                path.unlink(missing_ok=True)
                photo.state = "deleted"
                photo.deleted_at = dt.datetime.now(dt.timezone.utc)
                fixed += 1
    return {"fixed": fixed, "deleted_staging_rows": deleted}


def reconcile_progress_photos() -> dict:
    with photo_archive_lock():
        return _reconcile_progress_photos()
