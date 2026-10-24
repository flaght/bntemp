"""本地 Flow 的导入与多 seed 一致性回归；替换外部推理依赖。"""
import ast
from contextlib import contextmanager
from importlib import import_module
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'demos.10_hybrid_transformer'
adapter = import_module(PACKAGE + '.model_adapter')


@contextmanager
def local_flow_with_stubbed_dependencies():
    modules = {}

    def module(name, **attributes):
        value = ModuleType(name)
        value.__path__ = []
        value.__dict__.update(attributes)
        modules[name] = value
        return value

    # 单独的包对象避免真实包的子模块缓存被测试替身污染。
    package = module(PACKAGE)
    package.__path__ = [str(ROOT / 'demos/10_hybrid_transformer')]
    nn = module('torch.nn', Module=type('Module', (), {}))
    module('torch', nn=nn, Tensor=object,
           cuda=SimpleNamespace(is_available=lambda: False),
           device=lambda name: SimpleNamespace(type=name))
    module('pandas')
    module('numpy', isfinite=math.isfinite)
    for name in ('ultron', 'ultron.sentry', 'lumina', 'lumina.formual',
                 'lumina.evolution', 'lumina.evolution.fusion', 'lumina.genetic',
                 'lumina.genetic.signal', 'lumina.genetic.signal.method',
                 'kichaos', 'kichaos.nn'):
        module(name)
    module('ultron.sentry.api', __all__=[])
    module('lumina.env')
    module('lumina.formual.iactuator', Iactuator=lambda **kwargs: SimpleNamespace(factors=[]))
    module('lumina.evolution.fusion.actuator', Actuator=lambda **kwargs: SimpleNamespace())
    for name in ('linear', 'linear_threshold', 'threshold'):
        module('lumina.genetic.signal.method.' + name, **{name + '_signal': Mock()})
    module('kichaos.nn.HybridTransformer', HybridTransformer=object)
    module('kichaos.nn.HybridTransformer.masking', TriangularCausalMask=object)
    with patch.dict(sys.modules, modules):
        # 清除可能已加载的实际子模块，确保执行的是当前源码中的相对导入。
        sys.modules.pop(PACKAGE + '.flow', None)
        sys.modules.pop(PACKAGE + '.model', None)
        yield adapter._load_live_flow()


class FakeModel:
    def __init__(self, **architecture):
        self.architecture = architecture

    def load_state_dict(self, state, strict):
        if not strict:
            raise AssertionError('Checkpoint loading must remain strict')

    def to(self, device):
        return self

    def eval(self):
        return self


class LocalFlowTests(unittest.TestCase):
    def release(self, root, changed_field=None):
        checkpoints = {}
        for seed in (1, 2):
            config = dict(env_config={'seed': seed}, features=['feature_A'], lookback=20,
                          ret_scale_by_code={'P': .01}, loss_config={}, architecture={})
            if seed == 2 and changed_field:
                config[changed_field] = {
                    'features': ['feature_B'], 'lookback': 10,
                    'ret_scale_by_code': {'P': .02},
                    'loss_config': {'changed': True}, 'architecture': {'d_model': 128},
                }[changed_field]
            run = root / str(seed) / 'run'
            run.mkdir(parents=True)
            (run / 'config.json').write_text(json.dumps(config))
            checkpoints[seed] = dict(version='hybrid_transformer_loss_v1',
                **{key: config[key] for key in ('features', 'lookback', 'ret_scale_by_code', 'loss_config')},
                model_config=config['architecture'], model_state_dict={})
        return lambda path: checkpoints[int(path.parent.parent.parent.name)]

    def test_relative_loader_resolves_local_flow_and_local_model(self):
        with local_flow_with_stubbed_dependencies() as flow:
            self.assertEqual(flow.LiveFlow.__module__, PACKAGE + '.flow')
            self.assertEqual(flow.HybridTransformerRegressor.__module__, PACKAGE + '.model')
            self.assertIn('linear_signal', flow.SIGNAL_FUNCTIONS)

    def test_consistent_seeds_keep_first_reference_and_load_all_models(self):
        with TemporaryDirectory() as directory, local_flow_with_stubbed_dependencies() as flow:
            checkpoint = self.release(Path(directory))
            flow.feature_A = SimpleNamespace(_dependency=('close',))
            model = Mock(side_effect=FakeModel)
            with patch.object(flow, '_checkpoint', side_effect=checkpoint), \
                    patch.object(flow, '_factors_infos', return_value=[]), \
                    patch.object(flow, 'HybridTransformerRegressor', model):
                result = flow.LiveFlow(directory, 'directions.csv', 'P')
            self.assertEqual(result.features, ['feature_A'])
            self.assertEqual(result.lookback, 20)
            self.assertEqual(result.ret_scale, .01)
            self.assertEqual(set(result.models), {1, 2})
            self.assertEqual(model.call_count, 2)

    def test_seed_drift_rejected_before_loading_second_model(self):
        for field in ('features', 'lookback', 'ret_scale_by_code', 'loss_config', 'architecture'):
            with self.subTest(field=field), TemporaryDirectory() as directory, \
                    local_flow_with_stubbed_dependencies() as flow:
                checkpoint = self.release(Path(directory), field)
                model = Mock(side_effect=FakeModel)
                with patch.object(flow, '_checkpoint', side_effect=checkpoint), \
                        patch.object(flow, 'HybridTransformerRegressor', model):
                    with self.assertRaisesRegex(ValueError, '多 seed 配置不一致'):
                        flow.LiveFlow(directory, 'directions.csv', 'P')
                self.assertEqual(model.call_count, 1)

    def test_batch_prediction_contains_no_interactive_debugger(self):
        path = ROOT / 'demos/10_hybrid_transformer/flow.py'
        tree = ast.parse(path.read_text())
        batch = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == 'predict_batch')
        for node in ast.walk(batch):
            if isinstance(node, ast.Call):
                self.assertFalse(isinstance(node.func, ast.Name) and node.func.id == 'breakpoint')
                self.assertFalse(isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'pdb'
                    and node.func.attr == 'set_trace')


if __name__ == '__main__':
    unittest.main()
