"""三策略应用配置/离线入口规则；不加载SDK或联网。"""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.integration.shared_account import config, prepare_session, server, worker, preflight


class ProcessCliTests(unittest.TestCase):
    def test_three_modes_have_distinct_paths_and_default_does_not_connect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(path), "--environment", "replay",
                    "--execution-mode", "independent", "--trading-day", "20261014",
                    "--source-day", "20261013", "--replay-md-trading-day", "20261013"])
            document = config.read_config(path)
            self.assertEqual({r["kind"] for r in document["strategies"]},
                {"main_ema", "calendar_spread", "cross_section"})
            self.assertEqual(document["execution_mode"], "independent")
            files = {document["account_state_file"], *(r["state_file"] for r in document["strategies"])}
            self.assertEqual(len(files), 4)
            before = path.read_bytes()
            output = StringIO()
            with redirect_stdout(output):
                server.main(["--config", str(path), "--socket", str(Path(directory) / "sock"),
                             "--credentials-dir", str(Path(directory) / "keys")])
                worker.main(["--config", str(path), "--strategy-id", "calendar-cu-three",
                             "--credentials", str(Path(directory) / "not-created")])
                preflight.main(["--config", str(path)])
            self.assertEqual(output.getvalue().count("未连接"), 3)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(any(Path(p).exists() for p in files))
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_session.main(["--output", str(path), "--environment", "realtime",
                    "--execution-mode", "independent", "--trading-day", "20261014", "--source-day", "20261013"])

    def test_authorization_and_mode_are_not_inferred_from_connect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(path), "--environment", "realtime",
                    "--execution-mode", "netted", "--trading-day", "20261014", "--source-day", "20261013"])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                server.main(["--config", str(path), "--socket", "/tmp/test.sock", "--credentials-dir", "/tmp/test-keys"])
            document = json.loads(path.read_text())
            document["execution_mode"] = "independent"
            path.write_text(json.dumps(document))
            for entry, extra in ((server, ["--socket", "/tmp/test.sock", "--credentials-dir", "/tmp/test-keys"]),
                                 (worker, ["--strategy-id", "ema-cu-three", "--credentials", "/tmp/no-file"])):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    entry.main(["--config", str(path), "--connect", *extra])

    def test_dates_come_from_successful_readonly_observation_of_same_environment(self):
        with TemporaryDirectory() as directory:
            observed, output = Path(directory) / "observed.json", Path(directory) / "session.json"
            data = {"status": "observed", "orders_submitted": 0,
                "verification": "readonly_dates_and_contracts", "environment": "replay",
                "session_arguments": {"trading_day": "20261014", "source_day": "20261013",
                                      "replay_md_trading_day": "20261012"}}
            observed.write_text(json.dumps(data))
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(output), "--environment", "replay",
                    "--execution-mode", "independent", "--observations", str(observed)])
            self.assertEqual(config.read_config(output)["replay_md_trading_day"], "20261012")
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_session.main(["--output", str(Path(directory) / "wrong.json"), "--environment", "realtime",
                    "--execution-mode", "independent", "--observations", str(observed)])


if __name__ == "__main__":
    unittest.main()
