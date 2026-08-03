from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_compose_publishes_postgres_only_on_ipv4_loopback() -> None:
    docker = shutil.which("docker")
    assert docker is not None
    project_root = Path(__file__).resolve().parents[3]
    environment = {
        **os.environ,
        "POSTGRES_ADMIN_PASSWORD": "compose-test-admin",
        "TRADING_HOUSE_MIGRATION_PASSWORD": "compose-test-migrator",
        "TRADING_HOUSE_RUNTIME_PASSWORD": "compose-test-runtime",
    }

    completed = subprocess.run(  # noqa: S603
        [docker, "compose", "config", "--format", "json"],
        cwd=project_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    configuration = json.loads(completed.stdout)
    ports = configuration["services"]["postgres"]["ports"]

    assert ports == [
        {
            "mode": "ingress",
            "target": 5432,
            "published": "5432",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
