from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import doctor_escalate  # noqa: E402


REDIS_ENV_KEYS = (
    "DOCTOR_REDIS_URL",
    "REDIS_URL",
    "OVERSEER_GATEWAY_REDIS_URL",
    "CP_API_REDIS_URL",
    "RQ_REDIS_URL",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    "DOCTOR_REDIS_PASSWORD",
    "REDIS_PASSWORD",
    "RQ_REDIS_PASSWORD",
)


class FakeRedisClient:
    def ping(self) -> bool:
        return True


class FakeRedis:
    calls: list[tuple[str, object]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(("init", kwargs))

    @classmethod
    def from_url(cls, url: str, **kwargs):
        cls.calls.append(("from_url", {"url": url, **kwargs}))
        return FakeRedisClient()

    def ping(self) -> bool:
        return True


class DoctorEscalateRedisConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeRedis.calls = []
        self.redis_module_patch = mock.patch.dict(
            sys.modules,
            {"redis": types.SimpleNamespace(Redis=FakeRedis)},
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {key: "" for key in REDIS_ENV_KEYS},
            clear=False,
        )
        self.redis_module_patch.start()
        self.env_patch.start()
        for key in REDIS_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.redis_module_patch.stop()

    def test_get_redis_uses_authenticated_url_from_environment(self) -> None:
        os.environ["DOCTOR_REDIS_URL"] = "redis://:secret@example.test:6380/2"

        redis_conn = doctor_escalate._get_redis()

        self.assertIsInstance(redis_conn, FakeRedisClient)
        self.assertEqual(
            FakeRedis.calls,
            [
                (
                    "from_url",
                    {
                        "url": "redis://:secret@example.test:6380/2",
                        "socket_timeout": 3,
                    },
                )
            ],
        )

    def test_get_redis_reads_password_and_host_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "redis.env"
            env_file.write_text(
                "\n".join(
                    [
                        "REDIS_HOST=redis.internal",
                        "REDIS_PORT=6381",
                        "REDIS_DB=4",
                        "RQ_REDIS_PASSWORD=from-file",
                    ]
                )
            )
            with mock.patch.object(
                doctor_escalate,
                "_REDIS_ENV_FILE_PATHS",
                (env_file,),
                create=True,
            ):
                redis_conn = doctor_escalate._get_redis()

        self.assertIsInstance(redis_conn, FakeRedis)
        self.assertEqual(
            FakeRedis.calls,
            [
                (
                    "init",
                    {
                        "host": "redis.internal",
                        "port": 6381,
                        "db": 4,
                        "password": "from-file",
                        "socket_timeout": 3,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
