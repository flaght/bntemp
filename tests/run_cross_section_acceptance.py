"""04 SimNow集中离线验收；记录版本/日志/退出码，首个失败即停止。"""

import argparse
import hashlib
from importlib import import_module
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FOUR_VENUE_VERSION = "PF2026101208_REV2"
FOUR_VENUE_RUNTIME_HASHES = {
    "bomber/framework/dataprep/live_portfolio.py": "87bfe1d033b4b3efa4338010fbf368a9b262d71987738744da6a82071cf1e48c",
    "bomber/framework/market/stream/ctp/feed.py": "065a4effbabbf281f13c9d64723a2ae6d073bec6d01147894c147e20b081a34b",
    "bomber/framework/market/stream/ctp/converter.py": "157648c40bed2f5467cb9de68ad4ec2c8f971d62ebf10c77bd7f7c052ea7ee7e",
    "bomber/framework/trader/execution/builders.py": "54733c8ab2e7bb9713a2cd2bfcd4d9fe274c53479bc7eac6a8661b9428d2b37d",
    "bomber/framework/trader/execution/ctp/accounting.py": "6bb37ceb2bcad91b4c82f6a6cf77da0440cc8403867a3353f3417106c952f162",
    "bomber/framework/trader/execution/ctp/ledger.py": "97368cdee2e0411ff927e4bdb614e35b705980db5691cdbf488332bde135827e",
    "bomber/framework/trader/execution/ctp/planner.py": "3d119db17d95b85289ba905c1d7f1fa5a3d97d7d54e31fadd5cb7d2189a15f40",
    "bomber/framework/trader/execution/ctp/limit_planner.py": "52490c75d9464c8b2a96bfbcab0da30d1e1f8f2876d7e2421b342545a7d6aec7",
    "bomber/framework/trader/execution/ctp/native_driver.py": "620f3847b53fac28ab85e25805ab961c58ac2e672b78f4b5eebe3f6c85f6f798",
    "bomber/framework/trader/execution/ctp/td_transport.py": "75522647bc5b82226dbc1992e2958d4391f8920b724077062210c1dc42b71eb5",
    "bomber/framework/trader/runtime/ctp_account.py": "a404376dd009fd1f777a61f2683bf35723bf18b884da635aa316e94c2724fc6a",
    "demos/04_cross_section/run_live.py": "a27b235492ed6def166b29c4bc58593229881394df53b89670aa95dc16e96dea",
    "demos/04_cross_section/live_runtime.py": "d4bbad3600d3318faa87c4b708592a3a978d9ca18bc6a14fd568d4958e523bf1",
    "demos/04_cross_section/recovery.py": "c641001e9175fcc9aba5e214dd72eb581aad89bb33dc5e93c6987345b3883ce3",
    "tests/test_cross_section_live.py": "dad0dcaeca3da9a5e57cd4dbef059b4ea26809bab0376177d77d2219c1d5b2bc",
    "tests/test_live_common.py": "718871a1902621f8da124abd8019f70188599a6ee83ef59b5c2041adbb5cd5e5",
}
FEATURE_DEPLOYMENT_GROUPS = frozenset(("feature-datahub", "feature-query-live", "option-vega", "option-vega-live", "option-calendar", "option-shared"))
# 补充原生/08被测文件摘要；入口摘要由独立GAP08入口持有，不在此处自引用。
RETIRED_OPTION_SOURCES = ('demos/08_option_vega/configuration.py', 'demos/08_option_vega/continuous.py', 'demos/08_option_vega/features.py', 'demos/08_option_vega/acceptance.py', 'demos/08_option_vega/assembly.py', 'demos/08_option_vega/backtest_runtime.py', 'demos/08_option_vega/check_backtest_data.py', 'demos/08_option_vega/checkpoint.py', 'demos/08_option_vega/execution_audit.py', 'demos/08_option_vega/live_runtime.py', 'demos/08_option_vega/minute_diagnostics.py', 'demos/08_option_vega/replay_test.py', 'demos/08_option_vega/rollover.py', 'demos/08_option_vega/config/backtest.yaml')
FEATURE_RUNTIME_HASHES = {
 'tests/test_option_by_pos_native.py': '4e6b8ff486ce16ef082c4305a9ba2fc822ae01296bd279f6aa0d882e61047bc3',
 'tests/test_option_daily_settlements.py': 'e8e0102617a3882aeca61bfa4335d2581e5869be5494628d4a1dba095815d67c',

 'bomber/framework/dataprep/future_settlements.py': '536f19e524cb506cd6ac7857f2a09194d60ce3bb5f3c8a3613c1a97ee3b2cda6',
 'bomber/framework/dataprep/option_settlements.py': '9bd974d705409e0c8a77e950915d2e09b8a11fa41460b86a885dc8361b361960',
 'bomber/framework/trader/execution/attribution.py': 'e35b3006ce4ccbaeeb403c33ebecc4cd82a09100adb8031d2cf9aed3cb2a6b55',
 'bomber/framework/trader/execution/option_settlement.py': 'bc5ef19dd078fd25ae5a82d48ed134b3c7aa19d387bfc3a4d28707e1b2d24426',
 'bomber/framework/trader/execution/simulation/backend.py': 'cb6164f6d815655078e99e70942a7a108a2fb4fe2d413b8a076e889b61855509',
 'bomber/framework/trader/execution/simulation/client.py': 'd4b54f7e77cb2cb351ea9a5c8024b052f2938f445b024b5b37cc54d913a223d8',
 'bomber/framework/trader/execution/simulation/option_expiry.py': '89e517844850ef97bad834fd65e6742f5d144a45e6f6e53a7566a370c2345091',
 'bomber/framework/trader/instrument_factory.py': 'ceada319d4fcb15b815825b78c7b9aef35bba93c0aedd702a76b9a7a4edf6b0b',
 'tests/test_future_settlements.py': 'e1364eb0828fe0d80849056bf40e2414feff8cfbc8533734a0e8ff5ed842f577',
 'tests/test_option_expiry_module.py': '4e5ce0323ec4b347ae30d138907a1ba0bf93553dc28b0c373ef14988a1a39f51',
 'tests/test_option_vega_settlement.py': '47284fb86cc4536d582f5f374e4303512d913c3c716526c63b77110291f8a78b',
 'tests/test_simulation_market_timing.py': '93922d30e858e24f207cce8a6e555b78d6926fc38473df10c1ff37a9d9613a42',
'tests/test_option_vega_native.py': 'd7b599bc50f22c3a7c16db64180277065c61f468755ea76c99b7e5152a5adcf1',
 'tests/test_option_continuous.py': '633a1904c6c0b0ca6523be57fc7df7af05ef1a2940acf20f8c6caead40c419b5',
 'tests/test_option_daily_report.py': 'cf351fe2fac04ae31064ff4326d0e53bd0dcb9eeac8905d44980c8d4454ebee0',
 'demos/08_option_vega/config/market_dolphindb.json': '104380198b82e6fdb7f2bdca349cf36a497fcc663f528fb924406c579537c951',
 'demos/08_option_vega/config/vega_live.json': '325c0bb6f14eb4f6f7d6e4aad551986d4902a798de91c6341f44a2ee9e13a835',
 'demos/08_option_vega/config/vega_backtest.json': '643fc631d4baa6ca3a2c4fb4613c07f49d9f3069fb02ec86e123d1c76081b488',
 'demos/08_option_vega/reports.py': 'dc994e55d61f85ce89439750a950524a245799912f26b7cd156ee48752b12528',
 'bomber/framework/trader/runtime/reports.py': '4e8dd88e4aa2605be2637462c053b4d37e846b00318865c098b37d808065e0c8',
 'tests/test_cli_config.py': '6cb276ada5ee6f4a5e0ebd5cbc92016a544226b986f25700c8e38347d71f00a2',
 'demos/08_option_vega/config/strategy.yaml': '3f36304e789f4a10ed1143374b3f872c9624f13491c94856a53e464436dd6e3d',
 'bomber/framework/__init__.py': 'fcaca2c174a5e6b91f0aa28c17b2a4ded3b0e3aedd33925ed984cb5f43d8c2e7',
 'bomber/framework/requirements-config.txt': '865bbefc83398c2d53d4d494e8c0ad2302c93e657b78b860a1eba3ac71044309',
 'bomber/framework/cli.py': 'd8309e60c34edbac71d8f589f59d5baaab3c38ef79508d483f869bfa8a8f8c4d',
 'demos/08_option_vega/feature/index_tinysoft.json': '6454ff00e1759892b839908bf102f0599454fc8291492a6d85db1acaf92725a3',
 'demos/08_option_vega/feature/index_close.json': '06ba3d05ceb5822ff12181d9e38555ac6a8555602c06b4d57738bedd922d984d',
 'tests/test_option_dolphindb_dataframe_example.py': '1fd3861e279c45083a1bfed7103c89736a1b02e4ab27a23bd9376e45f4ac5990',
 'demos/08_option_vega/feature/example_dolphindb.py': 'e7969da5dcbfdaf0427deb59648c4d96fd09d9eec3314d90b8b4bc79f3285cb0',
 'demos/08_option_vega/feature/example_dataframe.py': 'b8c54b7592e1df7b4fcaf045e616a1bb7dd7186124c41bb22d53660dd70d9285',
 'tests/test_option_feature_dataframe.py': 'd93ab2e370fcfa50f1c9755b6e1ecea9d085a6ccd53e941cbc3d43faad28db55',
 'demos/08_option_vega/feature/dataframe.py': '410b21f0aae903e379ff71ffe637878984358ca58056a2201b74b9fae1c0066b',
 'demos/08_option_vega/feature/backtest.py': '275662c2e96259ce46953bd9af0f147b8e1f06188494c21357a93b57744fd3cf',
 'tests/test_option_feature_example.py': '34f2d696215d2018329df01a40b558219e12c9c91d0a700bf190ff9c03ff3f3b',
 'demos/08_option_vega/feature/index.json': 'eba898958f39a4235c4f62751d3e510ac0682fa1eac570a4bca6f43a25967470',
 'demos/08_option_vega/feature/generate.py': '2411eb349a25730b794fcc123e47f7bca7f937abb7293f95ba78582cef231a65',
 'bomber/framework/market/stream/ctp/converter.py': '157648c40bed2f5467cb9de68ad4ec2c8f971d62ebf10c77bd7f7c052ea7ee7e',
 'bomber/framework/trader/execution/builders.py': '54733c8ab2e7bb9713a2cd2bfcd4d9fe274c53479bc7eac6a8661b9428d2b37d',
 'bomber/framework/trader/execution/ctp/accounting.py': '6bb37ceb2bcad91b4c82f6a6cf77da0440cc8403867a3353f3417106c952f162',
 'bomber/framework/calendars/DateUtilities/Calendar.pyx': 'efcf365e9795bb1669527c3639b21d28d99627119f772ed8a219502c943543eb',
 'bomber/framework/calendars/__init__.py': 'c6eb5a3ca194fc16f19f2c8683ee544fe4ca24ad0590f1183a95ff9f63d4bd3d',
 'bomber/framework/calendars/trading.py': '4f64f92382a87e6206df65d97119b52faa756035765e9f3d808f45ba46f9577c',
 'bomber/framework/dataprep/calendar.py': 'ad97c156acf7dd915e9727e30110efb1e5826e41f3512051c423b7462771e7ec',
 'bomber/framework/dataprep/live_options.py': '3222662581df4c0729e2eb4a2e077c9d83938ba35b424cb49ad27481943c4f0c',
 'bomber/framework/market/stream/feature_query.py': '450c8865d47b0713e2ff963da2889b8802bd5774049b8e2f8a4e098aa4c76328',
 'bomber/framework/trader/runtime/live/channels/ctp_recovery.py': 'b67571a72c4594ff7ca0672980c7e61bc7178da564beb7979f000806c027f3e1',
 'bomber/framework/trader/runtime/live/channels/features.py': 'bc3fba69836682cd6cf5d71014c2f99cb6182f6011bb80fcfa5125f667c55288',
 'bomber/framework/trader/runtime/live/readiness.py': '125a27f8b9ad3ede9809fa0865dfac90f44572f7e979f22c1e115db19883e96b',
 'scripts/integration/options/recording.py': 'bd4f0b5ad0e1a406b328b615c91d0cb59c14d099c41f9aa87bb916878cca214b',
 'demos/08_option_vega/live.py': '3a8b629f6b1a928777bf72019e5b7d0a2d8e49b35f2e0504cf69885aaa5d412f',
 'demos/08_option_vega/recovery.py': '946a48795d8895b23f3000e222b327b54fd2221647965d8bac14dc9859b6d89b',
 'demos/08_option_vega/run_live.py': '187e13c531a430edb65540a02ee342f83f63fad18298da79c2816a918a8f327a',
 'demos/08_option_vega/strategy.py': 'b63ebab6ac541f179e9249734b4b17010f9d8798ba0ebe5cdc1266d62a44784f',
 'scripts/integration/options/__init__.py': 'be65a34d8f3388d3252d04a55411d216618753783cbba6d4cf40f8aa24690b02',
 'scripts/integration/reference_data/calendar_export.py': 'f6ac57245f7893d0cc7406173f573e466df143fb617de969c95f9c49c9e90555',
 'tests/run_option_vega_offline.py': '6a89139cb47e0104616223b7e75bf58c08912a198cfa75bcf10ecd1447027289',
 'tests/test_calendars_compiled.py': 'bb410784db982991974947cd52242e6800d46e0eecc55b8f57e1c3316abf3c54',
 'tests/test_index_query_feed.py': 'b10ce4d0dff231e61e58c37b36f1a2b7a08615c6bd56c69139837f8544c7fafa',
 'tests/test_live_readiness.py': '68be26427fa88d3821f1aa520c84749756f1793a77c96aad1d0214f19f2a0540',
 'tests/test_live_recovery.py': '3d536ee48fbcc9fda1a91de95d8f65b2350e1fec00ed8eebe5295db8062101c6',
 'tests/test_option_recording_acceptance.py': 'e1781fa8c4443936decbc48561eb4c6a1a54731e381ded7016eb428ff2f61ba7',
 'tests/test_option_vega_assembly.py': '669cbdf94bf747e90bd38c0077509c3fc077de3549f5add2d45a1387ce1cc18d',
 'tests/test_option_vega_live.py': '369cc96c277e0866e7eac3803e10b3c45a54de8639276b12691dc92755633f58',
 'tests/test_trading_calendar.py': 'bde86802148881d8fda9aa0020ec2e05c35fbf134fa0bc0f7fa23b9da0991ff4',
 'bomber/framework/market/stream/health.py': '9db27606e1292e9e5a35c23ea2a32de466a0622effd960868e68d6a44353b8d1',
 'bomber/framework/market/stream/ctp/feed.py': '065a4effbabbf281f13c9d64723a2ae6d073bec6d01147894c147e20b081a34b',
 'bomber/framework/dataprep/features/query.py': '3a94d8bc0735e64e60da485a1ea3683438c78946c8f6f2ffcffa4f9ef256523a',
 'tests/test_ctp_depth_health.py': '6ed1585e89550a26d05e79c3cf2438b8f2f497f5499ba1a0b1ad6ce83845bbc4',
 'scripts/integration/options/replay.py': '725692fbf24f300a9c190459672b465924d3a8564a8af1646cf434a583e393fb',
 'tests/test_option_replay_test.py': 'ff77f97036859fcfd101041eb7a1aab68bcb2235150d43f0c883c29fc42495dd',
 'bomber/framework/trader/execution/live/backend.py': '25077802b9cc6ff801275df1307370f2ad2cf155cc14c9a89fe81b76ee3c21aa',
 'tests/test_option_execution_audit.py': 'a6b16aba601e8a04dbc99a1b499f5121339e86f6a099b5c16e851186fd442919',
 'bomber/framework/market/native/dolphin/driver.py': '5051664e2b5be093431041cedd74de13b0cb87da5962dc04a44e15f8d388bac3',
 'bomber/framework/market/stream/dolphin/config.py': '3acd6a5a052f61adc3744612cf65e1edd3a538937cfc43d92e41943c411161b9',
 'bomber/framework/market/stream/dolphin/converter.py': 'e0cd771fca99a01242db9293c7a5b96771825dc7fac1b4ad34f477b30df8da58',
 'bomber/framework/market/stream/dolphin/feed.py': '6601b5c79f756ad863976742fc27a1700edfbe897ceaf411effcb77fb0e1172e',
 'bomber/framework/trader/runtime/live/channels/dolphin.py': 'e928b84ba28cd4c9e20defb69d5f584a55e87c2827a75f3f755e869e6d72c626',
 'tests/test_option_dolphin_stream.py': 'f6a8a46d1f9ec3e2179c607d23c1f6f98d0f4acc7c3ba2ca59301159c0ecf3ff',
 'scripts/integration/options/dolphin_stream_probe.py': '1b9b7555a0662217347612bcd2b02e61a5632e661c638d7fed14653eb1c407e8',
 'bomber/framework/trader/execution/ctp/limit_planner.py': '52490c75d9464c8b2a96bfbcab0da30d1e1f8f2876d7e2421b342545a7d6aec7',
 'bomber/framework/trader/execution/shared.py': '8c419afe212f762c659ae3d0eb767bbeaf34a158423dfca76f9b34c70e0643bc',
 'bomber/framework/trader/persistence.py': 'f093a42413126d7afd16bb1161604b7011347a11e779f1f2a0196f4b426e4dd6',
 'bomber/framework/trader/runtime/live/channels/ctp_rollover.py': '298dcef3ddcd2df09a2d6219cbeebc3714ba6ab90721704fd0e52009b404789a',
 'bomber/framework/trader/runtime/live/channels/ctp_shared.py': '57e8a3ef24c87b3bd8414919839c2b09c767bf5aa5769b4eb7c47a7fdd88341f',
 'bomber/framework/trader/runtime/live/shared.py': '217563c37070cc24f2084dad14f1daa6d794a6a9740f992ad12f85ea2553168b',
 'scripts/integration/options/shared.py': '72db9fd7a2a17a79c5893d6c3feee404333a1e764e349268c76eb88933010022',
 'scripts/integration/options/simnow_migrate.py': '9414e937f240f1717b577ef82b85e0df6632c6f4cd43d92627baaea01a268009',
 'scripts/integration/options/simnow_preflight.py': '5028abd4b2528845762b0896bcbb312d3c40daf212f1293d1f01d5d169a39635',
 'scripts/integration/shared_account/config.py': '91ddc9c40168e259657f054f935989380cf4d0e59576c23813cd2dbb748d3ffc',
 'scripts/integration/shared_account/factories.py': '58b611935822dd48968ef0896691e5dc5f522e5f707c51952bfee1d6c430023a',
 'scripts/integration/shared_account/preflight.py': 'ecbb8b93e6a7956f88c72d5bd639075d5c0f6bc6e895c10e8d36a22ea0cdf89b',
 'scripts/integration/shared_account/rollover.py': '9721068bc651ea1c1c71ea321f8d5b6cf937fe85aa99d2989fcbb80a210ce705',
 'scripts/integration/shared_account/run.py': '928a75cf8fdda45feca7a599eb8c02291cd3500cda3ebcfe9aa4730c9b08e3e2',
 'scripts/integration/shared_account/server.py': '2a9de6f1f89be23a52a340394a1be0cc2f9b4e02d7d1dbf5bc3642fd19d431ab',
 'scripts/integration/shared_account/worker.py': '9ae2098f8a497b4a2058292b4cafc3126f23425d188be4813a67dba72a29139e',
 'tests/test_option_shared_rollover.py': '86a2dd1d3551fc4e7b8c2c8828db453fc116895ab6b14fdaf601766e1e7a1d64',
 'tests/test_option_simnow_migrate.py': 'a7256b74943c985ec50239b80a804778b9b45e6e78454b0863b551d8c4ff0f72',
 'tests/test_option_simnow_preflight.py': 'cfc761c53c0a7082528272d660e636ff2100d70b2f13667fa282a4e4636ff8bd',
 'bomber/framework/trader/runtime/live/continuous.py': '9954bd03b3f79067bd112d186170375a37294deb4106188845d2a6ae6bcab627',
 'scripts/integration/shared_account/continuous.py': 'aa9c076ec4e24319490dac0ed4af6adba52119431a32bea94943ad58d0b6a18a',
 'tests/test_continuous_position_runtime.py': 'f8d107d205d5387b0b8aac958aaf181fdc22dfa0559aed6c991ce1df7ae56b17',
 'tests/test_shared_continuous.py': 'c6e688611bc984707fa8fd095acedb2f6f4a68bac6326925814380c1b33470e7',
 'bomber/framework/dataprep/session_inputs.py': '9632a472e6621418eb00f03872baf4635a7d3f51349f2c3045d1e189077f6ede',
 'bomber/framework/calendars/sessions.py': '3ae1627d66f50a44a3ea53bb9e953b2e981b3e3a947ef36fbc18704199fb540f',
 'bomber/framework/calendars/hours.py': '273d4ab3bb0da4dd28fc805d4bc8c50790b328309461a7ee36383dc935facda7',
 'demos/08_option_vega/vega_signal.py': '7407614b19422773acff714e65508bf668445c1dc1d567c40c68ef775001ccfe',
 'demos/08_option_vega/feature/__init__.py': '4346c583915c67812676b8e2d520e669ae5b26aebbda05976f44f9f5a5b8bb40',
 'demos/08_option_vega/run_backtest.py': 'ddda67f2a31058f1b912fbdd48f3d3f89e84f2f6e5c3819ddcc712cb4435848e',
 'scripts/integration/options/check_backtest_data.py': '75c1140f623f14ac08a4e7f337066462dfcea337d8b69cd99186df5a6f7fbbaa',
 'tests/test_option_feature_backtest.py': '679ad9d9f1803973c38e6fd6b0a23fc891309a773741256b9d78c9bca8df5366'}


def inspect_feature_deployment(root, expected, retired=()):
    """只读代码/配置字节；先识别混版，不导入原生组件或待测Demo。"""
    sources = {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if (root / relative).is_file() else None for relative in expected}
    return {"sources": sources,
        "mismatches": [{"path": relative, "expected": digest, "actual": sources[relative]}
            for relative, digest in expected.items() if sources[relative] != digest],
        "retired_files": [relative for relative in retired if (root / relative).exists()]}


CASES = {
    "feature-datahub": ("tests.run_feature_datahub",),
    "feature-query-live": ("unittest", "tests.test_index_query_feed"),
    "option-vega": ("unittest", "tests.test_demo_option_vega", "tests.test_option_vega_live"),
    "option-vega-live": ("unittest", "tests.test_option_vega_assembly", "tests.test_option_replay_test", "tests.test_option_execution_audit", "tests.test_option_dolphin_stream", "tests.test_option_recording_acceptance"),
    "option-shared": ("unittest", "tests.test_option_shared_rollover", "tests.test_option_simnow_migrate", "tests.test_option_simnow_preflight", "tests.test_continuous_position_runtime", "tests.test_shared_continuous"),
    "option-calendar": ("unittest", "tests.test_calendars_compiled", "tests.test_trading_calendar", "tests.test_option_recording_acceptance"),
    "execution-composition": ("tests.test_execution_composition",),
    "shared-account": ("tests.test_shared_account",),
    "ema-recovery": ("tests.test_main_ema_recovery",),
    "continuous": ("tests.test_main_ema_continuous",),
    "live-common": ("unittest", "tests.test_live_common", "tests.test_live_channels", "tests.test_live_recovery", "tests.test_execution_composition", "tests.test_live_readiness", "tests.test_ctp_depth_health"),
    "history-public": ("tests.run_live_history_offline",),
    "lifecycle-public": ("tests.run_live_lifecycle_offline",),
    "cross-section": ("tests.test_cross_section_live",),
    "signal": ("tests.test_demo_cross_section_signal",),
    "entry": ("tests.run_main_ema_live",),
    "routing": ("tests.run_main_ema_simnow", "--stage", "routing"),
    "ctp": ("tests.run_main_ema_simnow", "--stage", "ctp"),
    "assembly": ("tests.test_strategy_assembly",),
    "dispatch": ("tests.run_execution_dispatch",),
    "attribution": ("tests.test_position_attribution",),
    "ctp-autosave": ("tests.run_p4_ctp_autosave",),
    "ctp-recovery": ("tests.run_p4_ctp_recovery",),
    "ctp-driver": ("tests.run_p4_ctp_driver",),
}
SOURCES = ('bomber/framework/trader/runtime/live/lifecycle.py',
 'tests/test_live_lifecycle.py',
 'tests/run_live_lifecycle_offline.py',
 'bomber/framework/trader/runtime/live/history.py',
 'tests/test_live_history.py',
 'tests/run_live_history_offline.py',
 'bomber/framework/datahub/features/schema.py',
 'bomber/framework/dataprep/features/raw.py',
 'bomber/framework/dataprep/features/definition.py',
 'bomber/framework/dataprep/features/query.py',
 'bomber/framework/dataprep/features/configs/features.json',
 'bomber/framework/dataprep/features/configs/features.example.json',
 'tests/test_feature_definitions.py',
 'bomber/framework/dataprep/features/dolphin_query.py',
 'bomber/framework/market/stream/feature_query.py',
 'bomber/framework/trader/runtime/live/channels/features.py',
 'tests/test_dolphindb_index_features.py',
 'tests/test_index_query_feed.py',
 'scripts/integration/features/index_readonly.py',
 'scripts/integration/features/read_only.py',
 'bomber/framework/datahub/features/contracts.py',
 'bomber/framework/datahub/features/store.py',
 'bomber/framework/datahub/features/alignment.py',
 'bomber/framework/datahub/features/frames.py',
 'bomber/framework/datahub/features/replay.py',
 'bomber/framework/datahub/features/__init__.py',
 'bomber/framework/dataprep/features/base.py',
 'bomber/framework/dataprep/features/codec.py',
 'bomber/framework/dataprep/features/file.py',
 'bomber/framework/dataprep/features/database.py',
 'bomber/framework/dataprep/features/factory.py',
 'bomber/framework/dataprep/features/service.py',
 'bomber/framework/dataprep/features/samples.py',
 'bomber/framework/dataprep/features/__init__.py',
 'bomber/framework/datahub/temporal.py',
 'bomber/framework/datahub/__init__.py',
 'bomber/framework/dataprep/sources/policy.py',
 'tests/run_feature_datahub.py',
 'tests/test_feature_datahub.py',
 'demos/08_option_vega/strategy.py',
 'demos/08_option_vega/vega_signal.py',
 'demos/08_option_vega/run_backtest.py',
 'demos/08_option_vega/run_live.py',
 'demos/08_option_vega/live.py',
 'demos/08_option_vega/recovery.py',
 'bomber/framework/market/frames.py',
 'bomber/framework/market/stream/aggregation.py',
 'bomber/framework/dataprep/live_options.py',
 'bomber/framework/dataprep/metadata.py',
 'bomber/framework/trader/runner.py',
 'bomber/framework/trader/execution/ctp/planner.py',
 'bomber/framework/trader/execution/ctp/limit_planner.py',
 'bomber/framework/trader/execution/ctp/accounting.py',
 'bomber/framework/trader/execution/builders.py',
 'bomber/framework/trader/runtime/live/channels/dolphin.py',
 'tests/test_option_vega_live.py',
 'tests/test_option_vega_assembly.py',
 'tests/run_option_vega_offline.py',
 'bomber/framework/trader/execution/policy.py',
 'bomber/framework/trader/execution/fanout.py',
 'bomber/framework/trader/execution/account.py',
 'bomber/framework/trader/execution/shared.py',
 'bomber/framework/trader/runtime/live/shared.py',
 'bomber/framework/trader/runtime/live/channels/ctp_shared.py',
 'scripts/integration/shared_account/config.py',
 'scripts/integration/shared_account/factories.py',
 'scripts/integration/shared_account/run.py',
 'tests/test_shared_account.py',
 'bomber/framework/trader/execution/model.py',
 'bomber/framework/trader/execution/__init__.py',
 'bomber/framework/trader/runtime/live/local.py',
 'bomber/framework/trader/runtime/live/channels/ctp_market.py',
 'tests/test_execution_composition.py',
 'bomber/framework/trader/template.py',
 'bomber/framework/trader/portfolio.py',
 'bomber/framework/trader/execution/planner.py',
 'bomber/framework/trader/runtime/live/recovery.py',
 'bomber/framework/trader/runtime/live/channels/ctp_recovery.py',
 'demos/01_main_ema/recovery.py',
 'tests/test_live_recovery.py',
 'tests/test_main_ema_recovery.py',
 'scripts/integration/ctp_binance/run.py',
 'bomber/framework/trader/runtime/__init__.py',
 'bomber/framework/trader/runtime/direct.py',
 'bomber/framework/trader/runtime/live/__init__.py',
 'bomber/framework/trader/runtime/live/channels/__init__.py',
 'bomber/framework/trader/runtime/live/channels/binance.py',
 'bomber/framework/trader/runtime/live/channels/ctp.py',
 'bomber/framework/trader/runtime/live/contracts.py',
 'bomber/framework/trader/runtime/live/health.py',
 'bomber/framework/trader/runtime/live/profiles.py',
 'bomber/framework/trader/runtime/live/runtime.py',
 'tests/test_live_channels.py',
 'bomber/framework/trader/runtime/live_policy.py',
 'bomber/framework/trader/runtime/ctp_setup.py',
 'bomber/framework/trader/runtime/ctp_account.py',
 'bomber/framework/dataprep/live_cache.py',
 'bomber/framework/dataprep/live_cached_role.py',
 'bomber/framework/dataprep/live_portfolio.py',
 'bomber/framework/trader/live_roles.py',
 'tests/test_live_common.py',
 'demos/01_main_ema/checkpoint.py',
 'scripts/integration/reference_data/configs/session_overrides_cu_2026.csv',
 'bomber/framework/trader/runtime/trading_sessions.py',
 'tests/test_main_ema_continuous.py',
 'bomber/framework/market/basic/base.py',
 'bomber/framework/market/stream/ctp/feed.py',
 'bomber/framework/market/stream/ctp/converter.py',
 'bomber/framework/trader/runtime/ctp.py',
 'bomber/framework/trader/runtime/managed.py',
 'bomber/framework/trader/persistence.py',
 'bomber/framework/trader/assembly.py',
 'bomber/framework/dataprep/live_references.py',
 'bomber/framework/dataprep/live_role.py',
 'bomber/framework/trader/execution/ctp/native_driver.py',
 'bomber/framework/trader/execution/ctp/td_transport.py',
 'bomber/framework/trader/execution/attribution.py',
 'bomber/framework/trader/execution/live/client.py',
 'bomber/framework/trader/execution/live/controlled.py',
 'demos/04_cross_section/run_live.py',
 'demos/04_cross_section/live_runtime.py',
 'demos/04_cross_section/recovery.py',
 'demos/04_cross_section/strategy.py',
 'demos/04_cross_section/cross_section_signal.py',
 'demos/01_main_ema/run_live.py',
 'demos/01_main_ema/strategy.py',
 'tests/test_cross_section_live.py',
 'tests/run_main_ema_live.py',
 'tests/run_p4_ctp_td_transport.py',
 'tests/test_demo_cross_section_signal.py',
 'tests/run_cross_section_acceptance.py',
 'tests/run_main_ema_simnow.py',
 'tests/test_strategy_assembly.py',
 'tests/run_execution_dispatch.py',
 'tests/test_position_attribution.py',
 'tests/run_p4_ctp_autosave.py',
 'tests/run_p4_ctp_recovery.py',
 'tests/run_p4_ctp_driver.py',
 'bomber/framework/market/native/dolphin/driver.py',
 'bomber/framework/market/stream/dolphin/config.py',
 'bomber/framework/market/stream/dolphin/converter.py',
 'bomber/framework/market/stream/dolphin/feed.py',
 'tests/test_option_dolphin_stream.py',
 'scripts/integration/options/dolphin_stream_probe.py')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=tuple(CASES), action="append")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--report-dir", type=Path, default=ROOT / "tests/results")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout须为正")
    path = args.report_dir / f"cross-section-acceptance-{time.time_ns()}"
    path.mkdir(parents=True, exist_ok=False)
    report = {"python": sys.executable, "project": str(ROOT), "status": "running", "modules": [], "sources": {}}
    print(f"离线验收 Python={sys.executable} project={ROOT} report={path}", flush=True)
    try:
        if not args.only or "cross-section" in args.only:
            check = inspect_feature_deployment(ROOT, FOUR_VENUE_RUNTIME_HASHES)
            report["cross_section_deployment"] = {"version": FOUR_VENUE_VERSION, **check}
            report["sources"].update(check["sources"])
            for mismatch in check["mismatches"]:
                print(f"四交易所版本不一致: {mismatch['path']} expected={mismatch['expected']} "
                      f"actual={mismatch['actual']}", flush=True)
            if check["mismatches"]:
                raise RuntimeError("04四交易所交付版本不一致；先同步所列源码，再复测")
        if not args.only or FEATURE_DEPLOYMENT_GROUPS.intersection(args.only):
            from tests.run_feature_datahub import EXPECTED_SOURCE_HASHES, RETIRED_SOURCES, ACCEPTANCE_VERSION
            check = inspect_feature_deployment(ROOT, {**EXPECTED_SOURCE_HASHES, **FEATURE_RUNTIME_HASHES}, (*RETIRED_SOURCES, *RETIRED_OPTION_SOURCES))
            report["feature_deployment"] = {"version": ACCEPTANCE_VERSION, **check}
            report["sources"].update(check["sources"])
            for mismatch in check["mismatches"]:
                print(f"版本不一致: {mismatch['path']} expected={mismatch['expected']} actual={mismatch['actual']}", flush=True)
            if check["retired_files"]:
                print("须移除退役代码: " + ", ".join(check["retired_files"]), flush=True)
            if check["mismatches"] or check["retired_files"]:
                raise RuntimeError("特征/08交付版本不一致；先同步所列源码/配置及清理旧文件，再复测")
        for relative in SOURCES:
            if relative not in report["sources"]:
                source = ROOT / relative
                report["sources"][relative] = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
        for name in ("bomber.framework.trader.execution.builders", "bomber.framework.trader.runner",
                     "demos.04_cross_section.run_live", "demos.04_cross_section.strategy"):
            module = import_module(name)
            actual = Path(module.__file__).resolve()
            if not actual.is_relative_to(ROOT):
                raise RuntimeError(f"模块未加载本次项目源码: {name} {actual}")
            print(f"加载路径: {name}={actual} SHA256={hashlib.sha256(actual.read_bytes()).hexdigest()}", flush=True)
        entry = import_module("demos.04_cross_section.run_live")
        runtime = import_module("demos.04_cross_section.live_runtime")
        feed = import_module("bomber.framework.market.stream.ctp.feed")
        recovery = import_module("demos.04_cross_section.recovery")
        common_recovery = import_module("bomber.framework.trader.runtime.live.channels.ctp_recovery")
        if not issubclass(recovery.ResumeLifecycle, common_recovery.CtpResumeLifecycle):
            raise RuntimeError("部署版本不一致：04/recovery.py仍为旧恢复流程；同步"
                "doc/evidence/checksums/PF2026101037_REV2_SHA256.txt所列文件并核对摘要后再测试")
        expected_interfaces = (
            (entry, "parse_args", "04 run_live.parse_args"),
            (runtime.CrossSectionSessionRunner, "bar_is_current", "04 runner.bar_is_current"),
            (feed.CtpLiveDataFeed, "depth_observations", "CTP feed.depth_observations"),
        )
        for owner, attribute, label in expected_interfaces:
            if not hasattr(owner, attribute):
                raise RuntimeError(f"部署文件版本不一致，缺少{label}；同步PF2026100924清单全部文件并核对SHA后再测试")
    except Exception as error:
        report.update(status="failed", deployment_error=str(error))
        (path / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"部署/加载核对失败: {error}", flush=True)
        return 2
    for key, command in CASES.items():
        if args.only and key not in args.only:
            continue
        started = time.monotonic()
        try:
            completed = subprocess.run([sys.executable, "-u", "-m", *command], cwd=ROOT,
                timeout=args.timeout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            code, output = completed.returncode, completed.stdout
        except subprocess.TimeoutExpired as error:
            raw = error.stdout or ""
            output = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw) + "\n模块超时\n"
            code = 124
        (path / f"{key}.log").write_text(output, encoding="utf-8")
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
        row = {"module": key, "exit_code": code, "seconds": round(time.monotonic() - started, 3)}
        report["modules"].append(row)
        report["status"] = "failed" if code else "running"
        (path / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"模块结果: {row}", flush=True)
        if code:
            print(f"停止，仅复测失败组: python -u -m tests.run_cross_section_acceptance --only {key}", flush=True)
            return 1
    report["status"] = "passed"
    (path / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("全部选定离线模块通过；实际柜台Recording/组合成交仍须独立验收。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
