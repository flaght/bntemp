"""真实配置与只读连接装配的离线回归；两个用途使用不同的假服务器。"""
from dataclasses import replace
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from bomber.framework.dolphindb import DolphinDbConnectionConfig
from bomber.framework.dataprep.history import DolphinDbHistoryConfig, DolphinDbHistoryProvider
from bomber.framework.dataprep.sources import DolphinDbReferenceConfig, DolphinDbReferenceSource


LEGACY = dict(DDB_HOST="legacy", DDB_PORT="8849", DDB_USERNAME="legacy-user",
    DDB_PASSWORD="legacy-secret", DDB_ENABLE_SSL="true")
SPLIT = dict(DDB_MARKET_HOST="market", DDB_MARKET_PORT="8850",
    DDB_MARKET_USERNAME="market-user", DDB_MARKET_PASSWORD="market-secret",
    DDB_MARKET_ENABLE_SSL="false", DDB_REFERENCE_HOST="reference",
    DDB_REFERENCE_PORT="8851", DDB_REFERENCE_USERNAME="reference-user",
    DDB_REFERENCE_PASSWORD="reference-secret", DDB_REFERENCE_ENABLE_SSL="true")


class DolphinDbConnectionTests(unittest.TestCase):
    def test_market_history_and_daily_static_references_connect_to_separate_servers(self):
        with patch.dict(os.environ, {**LEGACY, **SPLIT}, clear=True):
            market = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=7)
            daily = DolphinDbReferenceConfig.from_env()
            static = DolphinDbReferenceConfig.from_env(database="dfs://bomber_static")
        self.assertEqual(daily.database, "dfs://bomber_daily")
        self.assertEqual(static.database, "dfs://bomber_static")
        history = DolphinDbHistoryConfig(market, "dfs://minutes", "shfe_1min")
        for connection, provider in ((market, DolphinDbHistoryProvider),
                (daily, DolphinDbReferenceSource), (static, DolphinDbReferenceSource)):
            sdk = Mock()
            sdk.connect.return_value = True
            factory = Mock(return_value=sdk)
            source = provider(history if connection is market else connection, session_factory=factory)
            with self.subTest(database=getattr(connection, "database", "dfs://minutes")):
                source.open()
                try:
                    is_market = connection is market
                    sdk.connect.assert_called_once_with("market" if is_market else "reference",
                        8850 if is_market else 8851,
                        "market-user" if is_market else "reference-user",
                        "market-secret" if is_market else "reference-secret", reconnect=False,
                        readTimeout=7 if is_market else 15, writeTimeout=7 if is_market else 15)
                    factory.assert_called_once_with(enableSSL=not is_market)
                finally:
                    source.close()
                sdk.close.assert_called_once()

    def test_legacy_variables_still_configure_both_purposes(self):
        with patch.dict(os.environ, LEGACY, clear=True):
            market = DolphinDbConnectionConfig.from_env(purpose="market")
            reference = DolphinDbReferenceConfig.from_env()
        for connection in (market, reference):
            self.assertEqual((connection.host, connection.port, connection.username,
                connection.password, connection.enable_ssl),
                ("legacy", 8849, "legacy-user", "legacy-secret", True))

    def test_partial_scoped_configuration_falls_back_to_legacy_per_field(self):
        environment = {**LEGACY, "DDB_MARKET_HOST": "market", "DDB_REFERENCE_PORT": "8851"}
        with patch.dict(os.environ, environment, clear=True):
            market = DolphinDbConnectionConfig.from_env(purpose="market")
            reference = DolphinDbReferenceConfig.from_env()
        self.assertEqual((market.host, market.port, market.password), ("market", 8849, "legacy-secret"))
        self.assertEqual((reference.host, reference.port), ("legacy", 8851))

    def test_scoped_only_configuration_uses_defaults_without_legacy(self):
        environment = {key: value for key, value in SPLIT.items()
            if not key.endswith(("_PORT", "_ENABLE_SSL"))}
        with patch.dict(os.environ, environment, clear=True):
            for purpose in ("market", "reference"):
                connection = DolphinDbConnectionConfig.from_env(purpose=purpose)
                self.assertEqual(connection.host, purpose)
                self.assertEqual(connection.port, 8848)
                self.assertFalse(connection.enable_ssl)

    def test_missing_credentials_never_fall_back_to_other_purpose(self):
        for purpose, other in (("market", "REFERENCE"), ("reference", "MARKET")):
            environment = {key: value for key, value in SPLIT.items() if key.startswith(f"DDB_{other}_")}
            with self.subTest(purpose=purpose), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, f"DDB_{purpose.upper()}_HOST"):
                    DolphinDbConnectionConfig.from_env(purpose=purpose)

    def test_explicit_empty_or_invalid_scoped_values_do_not_silently_use_legacy(self):
        for name, value in (("HOST", " "), ("USERNAME", ""), ("PASSWORD", ""),
                ("PORT", "0"), ("PORT", "65536"), ("PORT", "invalid")):
            with self.subTest(name=name, value=value), patch.dict(os.environ,
                    {**LEGACY, f"DDB_MARKET_{name}": value}, clear=True):
                with self.assertRaises(ValueError):
                    DolphinDbConnectionConfig.from_env(purpose="market")

    def test_credentials_remain_hidden_and_timeout_is_validated(self):
        with patch.dict(os.environ, SPLIT, clear=True):
            connection = DolphinDbConnectionConfig.from_env(purpose="market")
            for config in (connection, DolphinDbReferenceConfig.from_env()):
                self.assertNotIn(config.username, repr(config))
                self.assertNotIn(config.password, repr(config))
        for value in (0, 121, True):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                replace(connection, read_timeout_seconds=value)

    def test_feature_bundles_accept_market_connection_for_raw_and_standard_tables(self):
        from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG, bundle_from_definition
        with patch.dict(os.environ, SPLIT, clear=True):
            connection = DolphinDbConnectionConfig.from_env(purpose="market")
        definition = json.loads(DEFAULT_FEATURE_CONFIG.read_text())
        raw = bundle_from_definition(definition, connection).binding("index.minute").source_config
        self.assertIs(raw.connection, connection)
        definition["datasets"][0]["source"] = dict(backend="dolphindb",
            database="dfs://features", table="index_minutes")
        standard = bundle_from_definition(definition, connection).binding("index.minute").source_config
        self.assertEqual((standard.host, standard.port, standard.username, standard.password),
            ("market", 8850, "market-user", "market-secret"))

    def test_feature_readonly_defaults_to_market_and_can_select_reference(self):
        from bomber.framework.dataprep.features import DolphinDbRawFeatureProvider, FeatureProviderFactory
        from scripts.integration.features.read_only import main
        for purpose, arguments in (("market", []), ("reference", ["--dolphindb-purpose", "reference"])):
            provider = Mock(spec=DolphinDbRawFeatureProvider)
            provider.transport = Mock()
            provider.inspect.return_value = {"sample": []}
            with self.subTest(purpose=purpose), TemporaryDirectory() as directory, \
                    patch.dict(os.environ, SPLIT, clear=True), patch("dotenv.load_dotenv"), \
                    patch.object(FeatureProviderFactory, "create_for", return_value=provider) as factory, \
                    redirect_stdout(StringIO()):
                result = main(["--connect", "--inspect", "--dataset", "index.minute", "--key", "000852",
                    "--report-dir", directory, *arguments])
            self.assertEqual(result, 0)
            self.assertEqual(factory.call_args.args[2].connection.host, purpose)
            provider.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
