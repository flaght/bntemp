"""公共配置解析使用真实YAML与argparse，不加载行情、SDK或柜台。"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bomber.framework.cli import ConfigArgumentParser, load_config


class ConfigArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.yaml"

    def parser(self):
        parser = ConfigArgumentParser(prog="strategy")
        parser.add_argument("--start-day", type=date.fromisoformat, required=True)
        parser.add_argument("--data-root", type=Path, required=True)
        parser.add_argument("--nav", type=Decimal, default=Decimal("100"))
        parser.add_argument("--period", type=int, choices=(1, 5), default=1)
        parser.add_argument("--no-tearsheet", action="store_true")
        parser.add_argument("--feature", action="append", default=[])
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--calendar", type=Path)
        group.add_argument("--calendar-id")
        return parser

    def config(self, extra="", *, config_id="research"):
        self.path.write_text('experiment_setups:\n  research:\n'
            '    description: "业务说明，不作为启动参数"\n'
            '    start_day: "2026-08-13"\n'
            '    data_root: "data with spaces"\n' + extra, encoding="utf-8")
        return ["--config-file", str(self.path), "--config-id", config_id]

    def assert_invalid(self, parser, argv, text):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as result:
            parser.parse_args(argv)
        self.assertEqual(result.exception.code, 2)
        self.assertIn(text, output.getvalue())

    def test_yaml_supplies_required_inputs_and_real_types(self):
        args = self.parser().parse_args(self.config('    nav: "123456.7890123456789"\n'
            '    period: 5\n    no_tearsheet: true\n'))
        self.assertEqual(args.start_day, date(2026, 8, 13))
        self.assertEqual(args.data_root, Path("data with spaces"))
        self.assertEqual(args.nav, Decimal("123456.7890123456789"))
        self.assertEqual(args.period, 5)
        self.assertTrue(args.no_tearsheet)
        self.assertFalse(hasattr(args, "description"))
        self.assertEqual(args.config_file, self.path)

    def test_cli_overrides_config_and_repeated_values_replace_config_list(self):
        argv = self.config('    period: 5\n    feature: [ma20, ma60]\n    calendar: dates.csv\n')
        args = self.parser().parse_args([*argv, "--period=1", "--feature", "blend",
            "--calendar-id", "china.cffex"])
        self.assertEqual(args.period, 1)
        self.assertEqual(args.feature, ["blend"])
        self.assertIsNone(args.calendar)
        self.assertEqual(args.calendar_id, "china.cffex")

    def test_flags_false_and_boolean_optional_override_true_defaults(self):
        parser = self.parser()
        parser.set_defaults(no_tearsheet=True)
        parser.add_argument("--cache", action=argparse.BooleanOptionalAction, default=True)
        parser.add_argument("--run", action="store_false", default=True)
        argv = self.config('    no_tearsheet: false\n    cache: false\n    run: true\n')
        args = parser.parse_args(argv)
        self.assertFalse(args.no_tearsheet)
        self.assertFalse(args.cache)
        self.assertTrue(args.run)
        args = parser.parse_args([*argv, "--no-tearsheet", "--cache", "--run"])
        self.assertTrue(args.no_tearsheet)
        self.assertTrue(args.cache)
        self.assertFalse(args.run)

    def test_repeated_multivalue_count_and_negative_scalars(self):
        parser = self.parser()
        parser.add_argument("--weights", type=Decimal, nargs=2)
        parser.add_argument("--offset", type=int)
        parser.add_argument("--verbose", action="count", default=7)
        parser.set_defaults(feature=["default"])
        argv = self.config('    feature: [ma20, ma60]\n    weights: ["0.2", "0.8"]\n'
            '    offset: -2\n    verbose: 2\n')
        args = parser.parse_args(argv)
        self.assertEqual(args.feature, ["ma20", "ma60"])
        self.assertEqual(args.weights, [Decimal("0.2"), Decimal("0.8")])
        self.assertEqual(args.offset, -2)
        self.assertEqual(args.verbose, 2)
        self.assertEqual(parser.get_default("verbose"), 7)
        self.assertEqual(parser.get_default("feature"), ["default"])
        args = parser.parse_args(self.config('    feature: []\n'))
        self.assertEqual(args.feature, [])

    def test_required_boolean_group_and_conflicting_flags_are_checked(self):
        parser = self.parser()
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--model", action="store_true")
        group.add_argument("--recording", action="store_true")
        args = parser.parse_args(self.config('    model: true\n'))
        self.assertTrue(args.model)
        self.assertFalse(args.recording)
        self.assert_invalid(parser, self.config('    model: true\n    recording: true\n'),
            "not allowed with argument")
        args = parser.parse_args([*self.config('    model: true\n'), "--recording"])
        self.assertTrue(args.recording)
        self.assertFalse(args.model)

    def test_choices_dates_mutual_exclusion_and_unknown_keys_are_not_bypassed(self):
        for extra, message in (
            ('    period: 2\n', "invalid choice"),
            ('    start-day: "2026-99-99"\n', "配置参数重复"),
            ('    calendar: dates.csv\n    calendar_id: china.cffex\n', "not allowed with argument"),
            ('    perido: 5\n', "未声明参数"),
            ('    no_tearsheet: "false"\n', "布尔值"),
            ('    config_id: other\n', "未声明参数"),
        ):
            with self.subTest(extra=extra):
                self.assert_invalid(self.parser(), self.config(extra), message)
        self.path.write_text('experiment_setups:\n  research:\n'
            '    start_day: "2026-99-99"\n    data_root: data\n')
        self.assert_invalid(self.parser(), ["--config-file", str(self.path),
            "--config-id", "research"], "invalid fromisoformat value")

    def test_missing_file_id_required_values_and_malformed_yaml_fail(self):
        argv = ["--config-file", str(self.path), "--config-id", "research"]
        self.assert_invalid(self.parser(), argv, "不可读取")
        self.config()
        self.assert_invalid(self.parser(), [*argv[:-1], "missing"], "可选ID: research")
        self.assert_invalid(self.parser(), argv[:2], "须同时指定")
        self.assert_invalid(self.parser(), ["--config-id", "research"], "须同时指定")
        for content, message in (
            ("experiment_setups: [", "YAML配置解析失败"),
            ("experiment_setups: []", "非空映射"),
            ("experiment_setups:\n  research: []", "须为参数映射"),
            ("experiment_setups:\n  research: {}", "required"),
            ("experiment_setups:\n  research:\n    period: 1\n    period: 5", "重复键"),
            ("experiment_setups:\n  research: !!python/object:os.system {}", "YAML配置解析失败"),
        ):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assert_invalid(self.parser(), argv, message)

    def test_multiple_ids_and_underscore_cli_aliases(self):
        self.config('    period: 5\n')
        with self.path.open("a") as stream:
            stream.write('  other:\n    start_day: "2026-09-01"\n    data_root: other\n')
        self.assertEqual(load_config(self.path, "research")["period"], 5)
        args = self.parser().parse_args(["--config_file", str(self.path), "--config_id", "other"])
        self.assertEqual(args.start_day, date(2026, 9, 1))
        self.assertEqual(args.period, 1)

    def test_cli_and_help_do_not_require_yaml_or_mutate_parser_defaults(self):
        parser = self.parser()
        argv = ["--start-day", "2026-08-13", "--data-root", "data", "--period", "5"]
        with patch.dict("sys.modules", {"yaml": None}):
            self.assertEqual(parser.parse_args(argv).period, 5)
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
                parser.parse_args(["--config-file", "missing.yaml", "--help"])
            self.assertEqual(result.exception.code, 0)
            self.assert_invalid(parser, self.config(), "需要PyYAML")
        parser.parse_args(self.config('    period: 5\n'))
        self.assertEqual(parser.parse_args(argv[:-2]).period, 1)


if __name__ == "__main__":
    unittest.main()
