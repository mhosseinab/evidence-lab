"""Exercise deployment ordering without Docker, SSH, data changes or paid calls."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("deploy-vps.sh")
IMAGE = "ghcr.io/example/evidence-lab@sha256:" + "a" * 64


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="evidence-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "DEPLOY_TEST_LOG": str(self.log),
        }
        docker = self.bin / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['DEPLOY_TEST_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if 'run' in sys.argv and os.environ.get('DEPLOY_TEST_MIGRATION_FAIL'):\n"
            "    sys.exit(1)\n"
        )
        docker.chmod(0o700)
        curl = self.bin / "curl"
        curl.write_text("#!/usr/bin/env bash\n[[ -z ${DEPLOY_TEST_HEALTH_FAIL:-} ]]\n")
        curl.chmod(0o700)
        (self.root / ".env").write_text("POSTGRES_PASSWORD=fixture-only\n")
        (self.root / "runtime.yaml").write_text("fixture-only\n")

    def run_deployment(self, image=IMAGE):
        return subprocess.run(
            ["bash", str(SCRIPT), image, str(self.root)],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_rejects_mutable_tag_before_docker(self):
        result = self.run_deployment("ghcr.io/example/evidence-lab:latest")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.commands(), [])

    def test_missing_private_config_stops_before_docker(self):
        (self.root / "runtime.yaml").unlink()
        result = self.run_deployment()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.commands(), [])

    def test_migration_failure_preserves_running_release(self):
        marker = self.root / "deployed-image"
        marker.write_text("previous-release\n")
        self.environment["DEPLOY_TEST_MIGRATION_FAIL"] = "1"
        result = self.run_deployment()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "previous-release\n")
        self.assertFalse(any("up" in command and "api" in command for command in self.commands()))

    def test_readiness_failure_does_not_record_success(self):
        self.environment["DEPLOY_TEST_HEALTH_FAIL"] = "1"
        result = self.run_deployment()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "deployed-image").exists())

    def test_migrations_precede_service_update(self):
        result = self.run_deployment()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        migrate = next(index for index, command in enumerate(commands) if "run" in command)
        update = next(index for index, command in enumerate(commands) if "up" in command and "api" in command)
        self.assertLess(migrate, update)
        self.assertEqual((self.root / "deployed-image").read_text(), IMAGE + "\n")
        self.assertFalse(any("down" in command or "prune" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
