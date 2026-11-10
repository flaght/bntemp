"""验证账户应用复制到demos后的依赖闭合、直接执行及路径兼容。"""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from demos.shared_account import account
from bomber.framework import cli
from bomber.framework.account_config import read_account_config

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT/'demos/shared_account'


class DemosAccountDeploymentTests(unittest.TestCase):
    def test_execution_directory_contains_only_current_entry_and_configuration(self):
        files = {path.name for path in PACKAGE.iterdir() if path.is_file() and path.suffix != '.env'}
        self.assertEqual(files,{'account.py','account.json','account.env.example','launch.yaml','README.md'})
        self.assertNotIn('scripts.integration.shared_account',(PACKAGE/'account.py').read_text())
        runtime = ROOT/'bomber/framework/trader/runtime/live/account.py'
        for node in ast.walk(ast.parse(runtime.read_text())):
            if isinstance(node,ast.ImportFrom):
                self.assertFalse((node.module or '').startswith(('demos','scripts')))

    def test_direct_and_module_account_commands_run_without_old_scripts_available(self):
        bootstrap = '''
import importlib.abc
import runpy
import sys
class BlockOldAccount(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('scripts.integration.shared_account'):
            raise ImportError('旧账户脚本包不可用')
sys.meta_path.insert(0, BlockOldAccount())
target = sys.argv.pop(1)
mode = sys.argv.pop(1)
if mode == 'file':
    runpy.run_path(target, run_name='__main__')
else:
    runpy.run_module(target, run_name='__main__')
'''
        for mode,target,cwd in [('file',str(PACKAGE/'account.py'),'/tmp'),
                ('module','demos.shared_account.account',str(ROOT))]:
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable,'-c',bootstrap,target,mode,
                    '--config',str(PACKAGE/'account.json')],cwd=cwd,env=dict(os.environ),
                    capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('纯账户JSON通过',result.stdout)

    def test_account_paths_still_point_to_the_same_public_files(self):
        settings = read_account_config(PACKAGE/'account.json')
        self.assertEqual(settings['account_state_file'],str(PACKAGE/'results/account/state.json'))
        self.assertEqual(settings['account_env_file'],str(PACKAGE/'account.env'))
        self.assertEqual(settings['report_dir'],str(PACKAGE/'results/account'))

    def test_default_environment_lookup_still_uses_project_root(self):
        with patch.dict(os.environ,{},clear=True), patch('dotenv.load_dotenv') as load:
            cli.load_environment()
        load.assert_called_once_with(ROOT/'.env')


if __name__ == '__main__':
    unittest.main()
