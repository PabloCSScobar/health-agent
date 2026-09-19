from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from health_agent import scheduler
from health_agent.db.models import ProgressPhoto, Supplement
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.tools.photos import save_progress_photo
from tests.postgres_guard import (
    is_isolated_test_database,
    require_isolated_test_database,
)


_CONTAINER = os.environ.get("TEST_POSTGRES_CONTAINER")
_RESTORE_ENABLED = (
    is_isolated_test_database()
    and os.environ.get("RUN_BACKUP_RESTORE_TEST") == "1"
    and bool(_CONTAINER)
)


@unittest.skipUnless(
    _RESTORE_ENABLED,
    "wymaga APP_ENV=test, RUN_BACKUP_RESTORE_TEST=1 i TEST_POSTGRES_CONTAINER",
)
class BackupRestorePostgresTest(unittest.TestCase):
    def tearDown(self) -> None:
        require_isolated_test_database()
        with get_session() as session:
            session.execute(delete(ProgressPhoto))
            session.execute(delete(Supplement).where(Supplement.name.like("restore-test-%")))

    def test_database_and_photo_archive_can_be_restored(self) -> None:
        database_url = make_url(settings.database_url)
        restore_database = f"health_restore_{uuid.uuid4().hex[:12]}"
        marker = f"restore-test-{uuid.uuid4().hex}"
        original_run = subprocess.run
        restored_engine = None

        def dump_from_test_container(command, **kwargs):
            container_command = [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={database_url.password or ''}",
                _CONTAINER,
                "pg_dump",
                "--username",
                database_url.username,
                "--dbname",
                database_url.database,
                "--no-password",
            ]
            return original_run(
                container_command,
                capture_output=True,
                check=True,
            )

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                photos_dir = root / "photos"
                backups_dir = root / "backups"
                image_buffer = io.BytesIO()
                Image.new("RGB", (13, 11), color="orange").save(
                    image_buffer, format="PNG"
                )
                image_data = image_buffer.getvalue()
                with get_session() as session:
                    session.add(Supplement(name=marker, dose="1", active=True))
                with patch.object(settings, "progress_photos_dir", str(photos_dir)):
                    saved = save_progress_photo(
                        image_data,
                        captured_date=dt.date(2026, 9, 19),
                        view="front",
                        source="test",
                    )
                    with (
                        patch.object(scheduler.settings, "backup_enabled", True),
                        patch.object(scheduler.settings, "backup_dir", str(backups_dir)),
                        patch.object(
                            scheduler.settings,
                            "progress_photos_dir",
                            str(photos_dir),
                        ),
                        patch.object(scheduler.subprocess, "run", side_effect=dump_from_test_container),
                    ):
                        sql_backup = scheduler.backup_database()

                self.assertIsNotNone(sql_backup)
                photo_backup = next(backups_dir.glob("*.photos.tar.gz"))
                with tarfile.open(photo_backup, "r:gz") as archive:
                    manifest = json.load(archive.extractfile("manifest.json"))
                    self.assertEqual(len(manifest["photos"]), 1)
                    photo_name = manifest["photos"][0]["file"]
                    restored_photo = archive.extractfile(f"photos/{photo_name}").read()
                self.assertEqual(restored_photo, image_data)
                self.assertEqual(
                    hashlib.sha256(restored_photo).hexdigest(),
                    manifest["photos"][0]["sha256"],
                )

                create_sql = f'CREATE DATABASE "{restore_database}"'
                original_run(
                    [
                        "docker", "exec", "-e",
                        f"PGPASSWORD={database_url.password or ''}",
                        _CONTAINER, "psql", "--username", database_url.username,
                        "--dbname", "postgres", "--command", create_sql,
                    ],
                    capture_output=True,
                    check=True,
                )
                with gzip.open(sql_backup, "rb") as backup:
                    sql_dump = backup.read()
                original_run(
                    [
                        "docker", "exec", "-i", "-e",
                        f"PGPASSWORD={database_url.password or ''}",
                        _CONTAINER, "psql", "--username", database_url.username,
                        "--dbname", restore_database, "--set", "ON_ERROR_STOP=1",
                    ],
                    input=sql_dump,
                    capture_output=True,
                    check=True,
                )

                restored_url = database_url.set(database=restore_database)
                restored_engine = create_engine(restored_url, future=True)
                with Session(restored_engine) as session:
                    supplement = session.execute(
                        select(Supplement).where(Supplement.name == marker)
                    ).scalar_one()
                    photo = session.get(ProgressPhoto, saved["id"])
                    self.assertEqual(supplement.dose, "1")
                    self.assertEqual(photo.sha256, hashlib.sha256(image_data).hexdigest())
                    self.assertEqual(photo.state, "ready")
        finally:
            if restored_engine is not None:
                restored_engine.dispose()
            if _CONTAINER:
                drop_sql = f'DROP DATABASE IF EXISTS "{restore_database}" WITH (FORCE)'
                original_run(
                    [
                        "docker", "exec", "-e",
                        f"PGPASSWORD={database_url.password or ''}",
                        _CONTAINER, "psql", "--username", database_url.username,
                        "--dbname", "postgres", "--command", drop_sql,
                    ],
                    capture_output=True,
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
