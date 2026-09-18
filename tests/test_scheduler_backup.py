from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from health_agent import scheduler


class BackupDatabaseTest(unittest.TestCase):
    def test_uses_database_url_without_docker_socket(self) -> None:
        database_url = "postgresql+psycopg://backup_user:s3cret@db.example:5544/health"
        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.object(scheduler.settings, "database_url", database_url),
                patch.object(scheduler.settings, "backup_dir", tmp_dir),
                patch.object(scheduler.settings, "backup_enabled", True),
                patch.object(
                    scheduler.subprocess,
                    "run",
                    return_value=SimpleNamespace(stdout=b"-- synthetic dump --"),
                ) as run,
            ):
                result = scheduler.backup_database()

            backup_files = list(Path(tmp_dir).glob("health_agent_*.sql.gz"))
            self.assertEqual(len(backup_files), 1)
            self.assertEqual(result, backup_files[0])
            with gzip.open(backup_files[0], "rb") as backup:
                self.assertEqual(backup.read(), b"-- synthetic dump --")

            command = run.call_args.args[0]
            self.assertEqual(command[0], "pg_dump")
            self.assertNotIn("docker", command)
            self.assertEqual(
                command,
                [
                    "pg_dump",
                    "--host",
                    "db.example",
                    "--port",
                    "5544",
                    "--username",
                    "backup_user",
                    "--dbname",
                    "health",
                    "--no-password",
                ],
            )
            self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "s3cret")
            self.assertTrue(run.call_args.kwargs["capture_output"])
            self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
