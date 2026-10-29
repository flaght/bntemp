"""同进程检查的控制流测试；不加载原生 DLL。"""
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch


class SurfaceProbeTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/integration/lxgbn/wrapper_surface_probe.py'
        spec = importlib.util.spec_from_file_location('surface_probe_under_test', path)
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        library = self.directory / 'stgWrapper.pyd'
        library.write_bytes(b'synthetic DLL fixture, never loaded')
        def forbidden(*args, **kwargs):
            raise AssertionError('Do not call native methods in inspection')
        self.module = NS(__file__=str(library), Start=forbidden,
                         IPyStrategy=type('IPyStrategy', (), {n: forbidden for n in self.probe.METHODS}))

    def inspect(self, native_values):
        previous = list(sys.path)
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(self.probe.importlib, 'import_module', return_value=self.module), \
                patch.object(self.probe.runpy, 'run_path', return_value={
                    'collect_report': lambda: native_values}) as values_probe:
            result = self.probe.collect_report(self.directory, with_bomber=True)
        self.assertEqual(previous, sys.path)
        values_probe.assert_called_once()
        return result

    def test_wrapper_and_bomber_values_are_checked_together(self):
        values = {'native_values_ready': True}
        report = self.inspect(values)
        self.assertTrue(report['required_names_present'])
        self.assertTrue(report['coexistence_ready'])
        self.assertIs(report['bomber_values'], values)

    def test_wrapper_names_alone_do_not_pass_failed_bomber_values(self):
        report = self.inspect({'native_values_ready': False, 'checks': []})
        self.assertTrue(report['required_names_present'])
        self.assertFalse(report['coexistence_ready'])


if __name__ == '__main__':
    unittest.main()
