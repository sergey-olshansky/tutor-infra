"""Execute the production SSH step with a private fake key and a custom sites mount."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


class ProductionAIConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.sites = root / "custom-sites-volume"
        site = self.sites / "learn.example.test"
        site.mkdir(parents=True)
        self.config = site / "site_config.json"
        self.initial = {"zai_api_key": "previous-fake-key", "zai_model": "keep-this-model", "unrelated": True}
        self.config.write_text(json.dumps(self.initial))
        self.config.chmod(0o640)
        self.key = "fake-key with spaces $ and ' quotes"
        self.executable("ssh", """
            import subprocess,sys
            assert sys.argv[1:3] == ['-T', 'root@dummy-host']
            raise SystemExit(subprocess.run(['bash', '-c', ' '.join(sys.argv[3:])], stdin=sys.stdin).returncode)
        """)
        self.executable("docker", """
            import json,os,sys
            if sys.argv[1] == 'compose':
                assert sys.argv[-3:] == ['ps', '-q', 'backend']
                print('nondefault-production-backend')
            elif sys.argv[1:] == ['inspect', 'nondefault-production-backend']:
                print(json.dumps([{'Mounts':[{'Source':os.environ['TEST_SITES_DIR'], 'Destination':'/home/frappe/frappe-bench/sites'}]}]))
            else:
                raise SystemExit('Unexpected Docker command')
        """)
        # macOS base64 lacks GNU -w0; emulate its documented encoding behavior.
        self.executable("base64", "import base64,sys; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())")
        self.executable("curl", "print('200')")
        workflow = Path(__file__).resolve().parents[2] / ".github/workflows/deploy-production.yml"
        block = workflow.read_text().split("      - name: Site health check\n", 1)[1]
        block = block.split("      - name: Retain released and previous production images", 1)[0]
        self.script = textwrap.dedent(block.split("        run: |\n", 1)[1])
        self.script = self.script.replace("${{ secrets.PROD_URL }}", "https://learn.example.test")

    def executable(self, name, code):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(code).lstrip())
        path.chmod(0o755)

    def execute(self, **changes):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                   ZAI_API_KEY=self.key, PROD_HOST='dummy-host', PROD_SITE='learn.example.test',
                   TEST_SITES_DIR=str(self.sites))
        env.update(changes)
        return subprocess.run(['bash', '-c', self.script], env=env, capture_output=True, text=True)

    def test_multiline_python_survives_ssh_and_key_stays_off_stdout(self):
        before = self.config.stat()
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.config.read_text()), {**self.initial, 'zai_api_key': self.key})
        self.assertNotIn(self.key, result.stdout + result.stderr)
        after = self.config.stat()
        self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                         (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)))
        self.assertEqual(list(self.config.parent.glob('.site_config.*')), [])

    def test_empty_key_fails_without_changing_config(self):
        result = self.execute(ZAI_API_KEY='')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.config.read_text()), self.initial)

    def test_site_path_traversal_is_rejected(self):
        result = self.execute(PROD_SITE='../outside')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.config.read_text()), self.initial)


if __name__ == '__main__':
    unittest.main()
