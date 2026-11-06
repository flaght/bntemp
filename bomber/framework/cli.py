"""公共启动参数：显式CLI > YAML启动配置 > 受限JSON参数文件 > argparse默认值。

ConfigArgumentParser沿用标准argparse声明，不加载行情、账户或策略。
配置键使用参数dest（如start_day），也接受start-day；相对路径仍按运行目录解析。
YAML只在指定--config-file时加载，未知键、重复键或非法值均明确拒绝。
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys


def load_config(file_name, config_id):
    """读取一个实验配置；失败抛ValueError，不返回None继续启动。"""
    try:
        import yaml
    except ImportError:
        raise ValueError("YAML配置需要PyYAML；请用当前Python运行 python -m pip install PyYAML") from None

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("配置映射的键须为字符串")
            if key in result:
                raise ValueError(f"配置存在重复键: {key}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    path = Path(file_name).expanduser()
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=UniqueKeyLoader)
    except (OSError, UnicodeError) as error:
        raise ValueError(f"配置文件不可读取: {path} ({type(error).__name__})") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        position = "" if mark is None else f"，第{mark.line + 1}行第{mark.column + 1}列"
        raise ValueError(f"YAML配置解析失败: {path}{position}") from None
    if not isinstance(document, dict) or set(document) != {"experiment_setups"}:
        raise ValueError("配置顶层须为experiment_setups映射")
    setups = document["experiment_setups"]
    if not isinstance(setups, dict) or not setups:
        raise ValueError("experiment_setups须为非空映射")
    if config_id not in setups:
        raise ValueError(f"配置ID不存在: {config_id}；可选ID: {', '.join(setups)}")
    selected = setups[config_id]
    if not isinstance(selected, dict):
        raise ValueError(f"配置{config_id}须为参数映射")
    return selected


def _value_token(value):
    if value is None or isinstance(value, (dict, list, bool)):
        raise ValueError("参数值须为标量；开关使用布尔值，多值参数使用列表")
    return str(value)


def _parameters(values):
    result = {}
    for key, value in values.items():
        if key == "description":
            continue
        name = key.replace("-", "_")
        if name in result:
            raise ValueError(f"配置参数重复: {name}")
        result[name] = value
    return result


def _json_parameters(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON配置存在重复键: {key}")
            result[key] = value
        return result
    try:
        with Path(path).expanduser().open(encoding="utf-8") as stream:
            values = json.load(stream, object_pairs_hook=unique)
    except (OSError, UnicodeError):
        raise ValueError(f"JSON参数文件不可读取: {path}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON参数解析失败: {path}，第{error.lineno}行") from None
    if not isinstance(values, dict):
        raise ValueError("JSON参数配置须为参数映射")
    return _parameters(values)


def _config_tokens(parser, action, value):
    """转成CLI输入，由同一argparse执行type/choices/nargs/required校验。"""
    option = next((item for item in action.option_strings if item.startswith("--")), None)
    if option is None:
        raise ValueError(f"配置只支持已声明的长选项: {action.dest}")
    if isinstance(action, argparse.BooleanOptionalAction):
        if type(value) is not bool:
            raise ValueError(f"{action.dest}须为YAML布尔值true/false")
        parser.set_defaults(**{action.dest: None})
        return [option if value else next(item for item in action.option_strings if item.startswith("--no-"))]
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        if type(value) is not bool:
            raise ValueError(f"{action.dest}须为YAML布尔值true/false")
        # false也能覆盖default=True；仅有激活开关才参与argparse互斥检查。
        parser.set_defaults(**{action.dest: not action.const if value == action.const else value})
        return [option] if value == action.const else []
    if isinstance(action, argparse._CountAction):
        if type(value) is not int or value < 0:
            raise ValueError(f"{action.dest}须为非负整数")
        parser.set_defaults(**{action.dest: 0})
        return [option] * value
    if not isinstance(action, (argparse._StoreAction, argparse._AppendAction)):
        raise ValueError(f"配置不支持该参数action: {action.dest}")
    values = value if isinstance(action, argparse._AppendAction) else [value]
    if not isinstance(values, list):
        raise ValueError(f"可重复参数{action.dest}须使用列表")
    if isinstance(action, argparse._AppendAction):
        parser.set_defaults(**{action.dest: []})
    tokens = []
    for item in values:
        if action.nargs is None:
            tokens.append(f"{option}={_value_token(item)}")
        elif isinstance(item, list):
            tokens.extend([option, *(_value_token(part) for part in item)])
        else:
            raise ValueError(f"多值参数{action.dest}须使用列表")
    return tokens


class ConfigArgumentParser(argparse.ArgumentParser):
    """可直接替换ArgumentParser；普通CLI使用方式不变，不自动打印参数值。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._json_configs = {}
        self.add_argument("--config-file", "--config_file", type=Path,
            help="YAML启动参数文件；相对路径按当前运行目录解析")
        self.add_argument("--config-id", "--config_id", help="experiment_setups中的配置ID")

    def add_json_config_argument(self, option, *, fields, sequences=(), **kwargs):
        """声明一个JSON参数依赖；仅允许指定字段，复用原CLI类型与校验。"""
        action = self.add_argument(option, type=Path, **kwargs)
        self._json_configs[action.dest] = (frozenset(fields), frozenset(sequences))
        return action

    def parse_args(self, args=None, namespace=None):
        argv = list(sys.argv[1:] if args is None else args)
        # 帮助无需PyYAML、配置文件或任何业务输入，保留原始类型/choices展示。
        options = argv[:argv.index("--")] if "--" in argv else argv
        if any(item in ("-h", "--help") for item in options):
            return super().parse_args(argv, namespace)
        probe = deepcopy(self)
        probe._defaults.clear()
        for action in probe._actions:
            action.required, action.default = False, argparse.SUPPRESS
            action.type, action.choices = None, None
        for group in probe._mutually_exclusive_groups:
            group.required = False
        explicit, _ = probe.parse_known_args(argv)
        file_name, config_id = getattr(explicit, "config_file", None), getattr(explicit, "config_id", None)
        if file_name is None and config_id is None and not self._json_configs:
            return super().parse_args(argv, namespace)
        if (file_name is None) != (config_id is None):
            self.error("--config-file与--config-id须同时指定")
        try:
            selected = _parameters(load_config(file_name, config_id)) if file_name is not None else {}
            working = deepcopy(self)
            actions = {action.dest: action for action in working._actions if action.option_strings}
            inherited = {}
            for name, (allowed, sequences) in self._json_configs.items():
                path = getattr(explicit, name, selected.get(name, actions[name].default))
                if path is None:
                    continue
                parameters = _json_parameters(path)
                if set(parameters) - allowed:
                    raise ValueError(f"JSON参数文件包含未允许字段: {', '.join(sorted(set(parameters) - allowed))}")
                if inherited.keys() & parameters.keys():
                    raise ValueError("多个JSON参数文件重复声明同一参数")
                for key in sequences & parameters.keys():
                    if not isinstance(parameters[key], list):
                        raise ValueError(f"JSON参数{key}须使用列表")
                    parameters[key] = ",".join(_value_token(item) for item in parameters[key])
                inherited.update(parameters)
            selected = {**inherited, **{name: value for name, value in selected.items() if value is not None}}
            overridden = set(vars(explicit))
            # CLI选择另一互斥选项时，覆盖配置中的整组选择。
            for group in working._mutually_exclusive_groups:
                members = {action.dest for action in group._group_actions}
                if members & overridden:
                    overridden.update(members)
            tokens = []
            for name, value in selected.items():
                if name not in actions or name in {"help", "config_file", "config_id"}:
                    raise ValueError(f"配置包含未声明参数: {name}")
                if name not in overridden and value is not None:
                    tokens.extend(_config_tokens(working, actions[name], value))
        except ValueError as error:
            self.error(str(error))
        return argparse.ArgumentParser.parse_args(working, tokens + argv, namespace)
