"""Strict manifest/schema helpers for local plugin packages.

This module deliberately does not import FastAPI, read credentials, execute plugin code,
or contact a network. Passing validation is not a security approval.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
import copy
import json
import math
import re
from typing import Any, Callable


class ManifestError(ValueError):
    """User-controlled package metadata is malformed or unsafe."""


_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_RANGE_RE = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
_SAFE_PART_RE = re.compile(r"^[^/\\\x00]+$")
_ALLOWED_MANIFEST = {
    "schema_version", "id", "name", "version", "host_api", "description", "files",
    "entrypoint", "tools", "dependencies", "python_dependencies", "permissions", "skills",
    "mcp", "signature", "ui", "data_schema", "migrations", "config_schema",
}
_ALLOWED_ENTRY = {"type", "path", "callable"}
_ALLOWED_DEP = {"id", "version"}
_ALLOWED_TOOL = {"name", "description", "input_schema", "permissions", "targets"}
_ALLOWED_TARGET = {"permission", "argument", "kind"}
_ALLOWED_SIG = {"algorithm", "key_id", "signature"}
_ALLOWED_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items", "enum", "minimum",
    "maximum", "minLength", "maxLength", "minItems", "maxItems",
}


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "SemVer":
        if not isinstance(text, str):
            raise ManifestError("版本必须是字符串")
        match = _SEMVER_RE.fullmatch(text)
        if not match:
            raise ManifestError(f"不是有效 SemVer: {text!r}")
        pre = tuple(match.group(4).split(".")) if match.group(4) else ()
        if any(part.isdecimal() and len(part) > 1 and part.startswith("0") for part in pre):
            raise ManifestError("SemVer prerelease 数字不能有前导零")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), pre)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base + ("-" + ".".join(self.prerelease) if self.prerelease else "")

    def _cmp_key(self):
        # Release versions sort after prereleases for the same numeric triplet.
        return (self.major, self.minor, self.patch, (1,) if not self.prerelease else (0, self.prerelease))

    def __lt__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        if not self.prerelease and other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if left.isdecimal() and right.isdecimal():
                return int(left) < int(right)
            if left.isdecimal() != right.isdecimal():
                return left.isdecimal()
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ManifestError("id 必须是至少两段小写 dotted identifier")
    if len(value) > 200:
        raise ManifestError("id 过长")
    return value


def validate_relpath(value: Any, *, field: str = "路径") -> str:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise ManifestError(f"{field}必须是相对路径")
    if "\x00" in value or ":" in value.split("/")[0]:
        raise ManifestError(f"{field}包含非法字符")
    parts = value.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") or not _SAFE_PART_RE.fullmatch(part) for part in parts):
        raise ManifestError(f"{field}包含非法段")
    if "\\" in value:
        raise ManifestError(f"{field}必须使用正斜杠")
    return "/".join(parts)


def _strict_dict(obj: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ManifestError(f"{where}必须是对象")
    extra = set(obj) - allowed
    if extra:
        raise ManifestError(f"{where}存在未知字段: {sorted(extra)}")
    return obj


def validate_json_schema(schema: Any, where: str = "input_schema", depth: int = 0) -> None:
    if depth > 12:
        raise ManifestError(f"{where}嵌套过深")
    obj = _strict_dict(schema, _ALLOWED_SCHEMA_KEYS, where)
    typ = obj.get("type")
    if typ not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ManifestError(f"{where}.type 不支持")
    if "properties" in obj:
        if typ != "object" or not isinstance(obj["properties"], dict):
            raise ManifestError(f"{where}.properties 只适用于 object")
        for key, child in obj["properties"].items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ManifestError(f"{where}.properties 名称非法")
            validate_json_schema(child, f"{where}.properties.{key}", depth + 1)
    if "required" in obj:
        required = obj["required"]
        if typ != "object" or not isinstance(required, list) or not all(isinstance(x, str) for x in required):
            raise ManifestError(f"{where}.required 非法")
        if "properties" in obj and not set(required) <= set(obj["properties"]):
            raise ManifestError(f"{where}.required 包含未声明字段")
    if "items" in obj:
        if typ != "array":
            raise ManifestError(f"{where}.items 只适用于 array")
        validate_json_schema(obj["items"], f"{where}.items", depth + 1)
    if "additionalProperties" in obj and (typ != "object" or not isinstance(obj["additionalProperties"], bool)):
        raise ManifestError(f"{where}.additionalProperties 只接受 object 布尔值")
    if "enum" in obj:
        if not isinstance(obj["enum"], list) or not obj["enum"] or len(obj["enum"]) > 100:
            raise ManifestError(f"{where}.enum 非法")
        try:
            encoded = [canonical_json(value) for value in obj["enum"]]
            if len(encoded) != len(set(encoded)) or sum(map(len, encoded)) > 65536:
                raise ValueError("duplicate or oversized enum")
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"{where}.enum 必须是唯一的有限 JSON 值") from exc
    for key in ("minimum", "maximum"):
        if key in obj and (not isinstance(obj[key], (int, float)) or isinstance(obj[key], bool)
                           or (isinstance(obj[key], float) and not math.isfinite(obj[key]))):
            raise ManifestError(f"{where}.{key} 必须是有限数字")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in obj and (type(obj[key]) is not int or obj[key] < 0):
            raise ManifestError(f"{where}.{key} 必须是非负整数")
    for key in ("minimum", "maximum"):
        if key in obj and typ not in {"integer", "number"}:
            raise ManifestError(f"{where}.{key} 只适用于数字")
    for key in ("minLength", "maxLength"):
        if key in obj and typ != "string":
            raise ManifestError(f"{where}.{key} 只适用于字符串")
    for key in ("minItems", "maxItems"):
        if key in obj and typ != "array":
            raise ManifestError(f"{where}.{key} 只适用于数组")
    for low, high in (("minimum", "maximum"), ("minLength", "maxLength"), ("minItems", "maxItems")):
        if low in obj and high in obj and obj[low] > obj[high]:
            raise ManifestError(f"{where} 下限不能高于上限")


def parse_range(text: Any) -> tuple[tuple[str, SemVer], ...]:
    if not isinstance(text, str) or not text.strip():
        raise ManifestError("版本范围不能为空")
    checks = []
    for part in text.split():
        match = _RANGE_RE.fullmatch(part)
        if not match:
            raise ManifestError(f"不支持的版本范围: {text!r}")
        checks.append((match.group(1) or "=", SemVer.parse(match.group(2))))
    return tuple(checks)


def satisfies(version: str | SemVer, expression: str) -> bool:
    actual = version if isinstance(version, SemVer) else SemVer.parse(version)
    checks = []
    for op, wanted in parse_range(expression):
        if op == "^":
            upper = (SemVer(wanted.major + 1, 0, 0) if wanted.major else
                     SemVer(0, wanted.minor + 1, 0) if wanted.minor else SemVer(0, 0, wanted.patch + 1))
            checks.append(actual >= wanted and actual < upper)
        elif op == "~":
            checks.append(actual >= wanted and actual < SemVer(wanted.major, wanted.minor + 1, 0))
        elif op == ">=": checks.append(actual >= wanted)
        elif op == ">": checks.append(actual > wanted)
        elif op == "<=": checks.append(actual <= wanted)
        elif op == "<": checks.append(actual < wanted)
        else: checks.append(actual == wanted)
    return all(checks)


def _validate_tool(tool: Any, index: int) -> dict[str, Any]:
    obj = _strict_dict(tool, _ALLOWED_TOOL, f"tools[{index}]")
    if not isinstance(obj.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,100}", obj["name"]):
        raise ManifestError(f"tools[{index}].name 非法")
    if not isinstance(obj.get("description", ""), str) or len(obj.get("description", "")) > 2000:
        raise ManifestError(f"tools[{index}].description 非法")
    if "input_schema" not in obj:
        raise ManifestError(f"tools[{index}] 缺少 input_schema")
    validate_json_schema(obj["input_schema"], f"tools[{index}].input_schema")
    if obj["input_schema"]["type"] != "object":
        raise ManifestError("工具 input_schema 根必须是 object")
    permissions = obj.get("permissions", [])
    if not isinstance(permissions, list) or not all(isinstance(x, str) and 0 < len(x) <= 100 for x in permissions):
        raise ManifestError(f"tools[{index}].permissions 非法")
    targets = obj.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 30:
        raise ManifestError(f"tools[{index}].targets 非法")
    for target in targets:
        t = _strict_dict(target, _ALLOWED_TARGET, f"tools[{index}].targets")
        if not isinstance(t.get("permission"), str) or not isinstance(t.get("argument"), str):
            raise ManifestError("tool target 需要 permission/argument")
        if t.get("kind") not in {"url", "path", "string"}:
            raise ManifestError("tool target.kind 非法")
        properties = obj["input_schema"].get("properties", {})
        if t["argument"] not in properties or properties[t["argument"]].get("type") != "string":
            raise ManifestError("tool target 必须绑定已声明的字符串参数")
        if t["permission"] not in permissions:
            raise ManifestError("tool target.permission 必须在工具 permissions 声明")
    return obj


_UI_ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_UI_SOURCE = re.compile(r'^(config|state)\.([a-z][a-z0-9_-]{0,63})$')
_UI_SECRET = re.compile(r'(?i)(api[_-]?key|password|secret|access[_-]?token|credential)')


def _ui_finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_ui_value(field: dict[str, Any], value: Any, *, required: bool = False) -> None:
    """Validate one declared primitive, never coerce strings/booleans/numbers."""
    if value is None:
        if required:
            raise ManifestError('UI 必填字段不能为空')
        return
    typ = field['type']
    if typ in ('text', 'textarea', 'select'):
        if not isinstance(value, str) or len(value) > field.get('max_length', 4000):
            raise ManifestError('UI 字符串类型或长度无效')
        if required and not value.strip():
            raise ManifestError('UI 必填字段不能为空')
        if typ == 'select' and value not in field['options']:
            raise ManifestError('UI select 值未声明')
    elif typ == 'number':
        if not _ui_finite(value):
            raise ManifestError('UI number 必须为有限数值')
        if value < field.get('min', -math.inf) or value > field.get('max', math.inf):
            raise ManifestError('UI number 超出范围')
    elif typ == 'boolean':
        if type(value) is not bool:
            raise ManifestError('UI boolean 必须为布尔值')
    elif typ == 'readonly':
        if type(value) not in (str, int, float, bool) or type(value) in (int, float) and not _ui_finite(value):
            raise ManifestError('UI readonly 只接受有限primitive')
        if isinstance(value, str) and len(value) > field.get('max_length', 4000):
            raise ManifestError('UI readonly 超长')


def validate_ui(raw: Any, tools: list[dict[str, Any]]) -> dict[str, Any]:
    obj = _strict_dict(raw, {'schema_version', 'title', 'description', 'fields', 'actions'}, 'ui')
    try:
        if len(canonical_json(obj).encode('utf-8')) > 32768:
            raise ManifestError('UI schema超过32KiB')
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError('UI schema不是有限JSON') from exc
    if type(obj.get('schema_version')) is not int or obj['schema_version'] != 1:
        raise ManifestError('ui.schema_version 仅支持1')
    if not isinstance(obj.get('title'), str) or not 1 <= len(obj['title'].strip()) <= 200:
        raise ManifestError('ui.title 非法')
    if 'description' in obj and (not isinstance(obj['description'], str) or len(obj['description']) > 2000):
        raise ManifestError('ui.description 非法')
    fields, actions = obj.get('fields'), obj.get('actions')
    if not isinstance(fields, list) or len(fields) > 50 or not isinstance(actions, list) or len(actions) > 30:
        raise ManifestError('ui fields/actions 需为有界列表')
    declared = {}
    source_names = set()
    for field in fields:
        f = _strict_dict(field, {'id', 'label', 'type', 'required', 'default', 'options', 'min', 'max', 'max_length', 'source'}, 'ui.fields')
        if not isinstance(f.get('id'), str) or not _UI_ID.fullmatch(f['id']) or f['id'] in declared or f['id'] in {'constructor','prototype','__proto__'} or _UI_SECRET.search(f['id']):
            raise ManifestError('UI field id非法/重复/凭据字段')
        if not isinstance(f.get('label'), str) or not 1 <= len(f['label'].strip()) <= 200:
            raise ManifestError('UI label非法')
        if f.get('type') not in ('text', 'textarea', 'number', 'boolean', 'select', 'readonly'):
            raise ManifestError('UI field type不支持')
        if 'required' in f and type(f['required']) is not bool:
            raise ManifestError('UI required需布尔值')
        for key in ('min', 'max'):
            if key in f and (f['type'] != 'number' or not _ui_finite(f[key])):
                raise ManifestError('UI min/max仅有限数字')
        if 'min' in f and 'max' in f and f['min'] > f['max']:
            raise ManifestError('UI 数字范围倒置')
        if 'max_length' in f and (f['type'] not in ('text', 'textarea', 'select', 'readonly') or type(f['max_length']) is not int or not 1 <= f['max_length'] <= 16000):
            raise ManifestError('UI max_length非法')
        if f['type'] == 'select':
            options = f.get('options')
            if not isinstance(options, list) or not 1 <= len(options) <= 100 or any(not isinstance(v, str) or not 1 <= len(v) <= 300 for v in options) or len(set(options)) != len(options):
                raise ManifestError('UI select options需唯一字符串列表')
        elif 'options' in f:
            raise ManifestError('UI options仅用于select')
        if 'source' in f:
            source = f['source']
            if not isinstance(source, str) or not _UI_SOURCE.fullmatch(source) or _UI_SECRET.search(source) or source.split('.')[-1] in {'constructor','prototype','__proto__'} or source in source_names:
                raise ManifestError('UI source仅安全的config/state单层唯一字段')
            source_names.add(source)
        if 'default' in f:
            validate_ui_value(f, f['default'], required=f.get('required', False))
        declared[f['id']] = f
    tool_names = {tool['name'] for tool in tools}
    action_names = set()
    for action in actions:
        act = _strict_dict(action, {'id', 'label', 'kind', 'tool', 'arguments'}, 'ui.actions')
        if not isinstance(act.get('id'), str) or not _UI_ID.fullmatch(act['id']) or act['id'] in action_names or act['id'] in {'constructor','prototype','__proto__'}:
            raise ManifestError('UI action id非法或重复')
        action_names.add(act['id'])
        if not isinstance(act.get('label'), str) or not 1 <= len(act['label'].strip()) <= 200:
            raise ManifestError('UI action label非法')
        if act.get('kind') not in ('tool', 'save_config', 'save_state'):
            raise ManifestError('UI action kind不支持')
        if act['kind'] == 'tool':
            if not isinstance(act.get('tool'), str) or act['tool'] not in tool_names:
                raise ManifestError('UI tool只能调用同包已声明工具')
            mapping = act.get('arguments', {})
            if not isinstance(mapping, dict) or len(mapping) > 100 or any(not isinstance(k, str) or not 1 <= len(k) <= 100 or not isinstance(v, str) or v not in declared for k, v in mapping.items()):
                raise ManifestError('UI arguments仅顶层参数到field id的映射')
        elif 'tool' in act or 'arguments' in act:
            raise ManifestError('UI save动作不接受tool/arguments')
    return obj


def config_form(schema):
    obj=_strict_dict(schema,{'type','properties','required','additionalProperties','title','description'},'config_schema')
    if obj.get('type')!='object' or obj.get('additionalProperties') is not False or not isinstance(obj.get('properties'),dict) or len(obj['properties'])>50:
        raise ManifestError('config_schema 需为最多50项、禁止额外字段的对象')
    required=obj.get('required',[])
    if not isinstance(required,list) or not all(isinstance(key,str) for key in required) or not set(required)<=set(obj['properties']):raise ManifestError('配置必填字段无效')
    fields=[]
    for key,value in obj['properties'].items():
        spec=_strict_dict(value,{'type','title','description','default','enum','minimum','maximum','minLength','maxLength'},'config_schema.properties')
        if spec.get('type') not in ('string','boolean','number','integer'):raise ManifestError('配置表单仅支持字符串、数字、布尔和字符串枚举，不接受脚本或远程引用')
        validate_json_schema({name:entry for name,entry in spec.items() if name not in ('title','description','default')})
        field={'id':key,'label':spec.get('title',key),'type':{'string':'text','boolean':'boolean','number':'number','integer':'number'}[spec['type']],
               'source':'config.'+key,'required':key in required}
        if 'enum' in spec:
            if spec['type']!='string' or not all(isinstance(entry,str) for entry in spec['enum']):raise ManifestError('配置表单枚举仅接受字符串')
            field.update(type='select',options=spec['enum'])
        for old,new in (('minimum','min'),('maximum','max'),('maxLength','max_length'),('default','default')):
            if old in spec:field[new]=spec[old]
        fields.append(field)
    return validate_ui({'schema_version':1,'title':obj.get('title','插件配置'),'description':obj.get('description','由插件声明的普通配置；保存仍需预览确认'),
                        'fields':fields,'actions':[{'id':'save_config','label':'预览保存配置','kind':'save_config'}]},[])


def validate_config(schema,value):
    form=config_form(schema)
    if not isinstance(value,dict) or not set(value)<=set(schema['properties']):raise ManifestError('配置含未声明字段')
    for field in form['fields']:
        if field['id'] not in value:
            if field.get('required'):raise ManifestError('缺少配置：'+field['label'])
            continue
        actual=value[field['id']];definition=schema['properties'][field['id']]
        validate_ui_value(field,actual,required=field.get('required',False))
        if definition['type']=='integer' and type(actual) is not int:raise ManifestError('配置需为整数：'+field['label'])
        if isinstance(actual,str) and len(actual)<definition.get('minLength',0):raise ManifestError('配置文字过短：'+field['label'])


def validate_data_migrations(manifest: dict[str, Any]) -> None:
    """Validate declarations only; absence remains unknown, never inferred from SemVer.

    Every migration is a direct, unambiguous upgrade from one known data schema to
    this package's explicitly declared target. Execution, staging limits, approval,
    activation and rollback policy belong to the trusted host, not package fields.
    This helper never mutates the caller or imports/executes the declared module.
    """
    if "data_schema" in manifest:
        schema = manifest["data_schema"]
        if type(schema) is not int or not 0 <= schema <= 2147483647:
            raise ManifestError("data_schema 必须是0..2147483647的明确整数；缺省表示未知")
    if "migrations" not in manifest:
        return
    if "data_schema" not in manifest:
        raise ManifestError("migrations 必须同时明确声明 data_schema，不能从应用版本推断")
    migrations = manifest["migrations"]
    if type(migrations) is not list or len(migrations) > 16:
        raise ManifestError("migrations 必须是至多16项的精确列表")
    sources = set()
    for index, migration in enumerate(migrations):
        where = f"migrations[{index}]"
        if type(migration) is not dict:
            raise ManifestError(f"{where} 必须是精确声明对象")
        item = _strict_dict(migration, {"from_schema", "to_schema", "entrypoint"}, where)
        if set(item) != {"from_schema", "to_schema", "entrypoint"}:
            raise ManifestError(f"{where} 必须完整声明 from_schema/to_schema/entrypoint")
        for name in ("from_schema", "to_schema"):
            if type(item[name]) is not int or not 0 <= item[name] <= 2147483647:
                raise ManifestError(f"{where}.{name} 必须是0..2147483647的明确整数")
        if not item["from_schema"] < item["to_schema"] == manifest["data_schema"]:
            raise ManifestError(f"{where} 仅允许升级至当前 data_schema，禁止下降/同版本或中间目标")
        if item["from_schema"] in sources:
            raise ManifestError("migrations 的 from_schema 不得重复或分叉")
        sources.add(item["from_schema"])
        if type(item["entrypoint"]) is not dict:
            raise ManifestError(f"{where}.entrypoint 必须是精确对象")
        entry = _strict_dict(item["entrypoint"], _ALLOWED_ENTRY, where + ".entrypoint")
        if (set(entry) != _ALLOWED_ENTRY or type(entry.get("type")) is not str or entry["type"] != "python"
                or type(entry.get("callable")) is not str
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", entry["callable"])):
            raise ManifestError(f"{where}.entrypoint 必须是 python/path/ASCII callable")
        path = validate_relpath(entry.get("path"), field=where + ".entrypoint.path")
        # Migration workers use an independent entry and Windows-safe exact names;
        # do not accept a declaration the strict worker would interpret differently.
        if type(entry.get("path")) is not str or len(path) > 240:
            raise ManifestError(f"{where}.entrypoint.path 必须是至多240字符的规范路径")
        for part in path.split("/"):
            if (part.endswith((" ", "."))
                    or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                    or any(ord(char) < 32 or char in ':<>"|?*' for char in part)):
                raise ManifestError(f"{where}.entrypoint.path 包含 Windows 不安全路径段")
        files = manifest.get("files", {})
        if (type(files) is not dict or not path.endswith(".py") or path not in files
                or type(files[path]) is not str or not re.fullmatch(r"[0-9a-f]{64}", files[path])):
            raise ManifestError(f"{where}.entrypoint.path 必须指向 files 中已哈希的 .py 文件")


def validate_manifest(raw: Any, *, file_names: set[str] | None = None) -> dict[str, Any]:
    obj = copy.deepcopy(_strict_dict(raw, _ALLOWED_MANIFEST, "manifest"))
    for key in ("schema_version", "id", "name", "version", "host_api", "files", "tools"):
        if key not in obj:
            raise ManifestError(f"manifest 缺少 {key}")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise ManifestError("只支持 schema_version=1")
    validate_id(obj["id"])
    if not isinstance(obj["name"], str) or not obj["name"].strip() or len(obj["name"]) > 200:
        raise ManifestError("name 非法")
    SemVer.parse(obj["version"])
    # host_api is a range, not a free-form label.
    if not isinstance(obj["host_api"], str):
        raise ManifestError("host_api 必须是 SemVer 范围")
    parse_range(obj["host_api"])
    files = obj["files"]
    if not isinstance(files, dict) or len(files) > 10_000:
        raise ManifestError("files 必须是有限对象")
    folded = set()
    for path, digest in files.items():
        normalized = validate_relpath(path, field="files 路径")
        if normalized != path or path == "manifest.json":
            raise ManifestError("files 路径必须是规范正斜杠形式且不能包含 manifest.json")
        if normalized.casefold() in folded:
            raise ManifestError("files 存在大小写重名")
        folded.add(normalized.casefold())
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestError(f"files.{path} 必须是小写 SHA-256")
    if file_names is not None and set(files) != set(file_names):
        raise ManifestError("manifest files 与实际文件集合不一致")
    validate_data_migrations(obj)
    if "description" in obj and (not isinstance(obj["description"], str) or len(obj["description"]) > 5000):
        raise ManifestError("description 非法")
    entry = obj.get("entrypoint")
    if entry is not None:
        entry = _strict_dict(entry, _ALLOWED_ENTRY, "entrypoint")
        if entry.get("type") != "python" or not isinstance(entry.get("path"), str) or not isinstance(entry.get("callable"), str):
            raise ManifestError("entrypoint 必须是 python/path/callable")
        validate_relpath(entry["path"], field="entrypoint.path")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", entry["callable"]):
            raise ManifestError("entrypoint.callable 非法")
        if file_names is not None and entry["path"] not in file_names:
            raise ManifestError("entrypoint.path 不在 files 中")
    deps = obj.get("dependencies", [])
    if not isinstance(deps, list) or len(deps) > 100:
        raise ManifestError("dependencies 非法")
    dependency_ids = set()
    for dep in deps:
        d = _strict_dict(dep, _ALLOWED_DEP, "dependencies")
        validate_id(d.get("id"))
        if d["id"] in dependency_ids:
            raise ManifestError("dependencies ID 重复")
        dependency_ids.add(d["id"])
        parse_range(d.get("version"))
    python_deps = obj.get("python_dependencies", [])
    if not isinstance(python_deps, list) or len(python_deps) > 100 or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in python_deps):
        raise ManifestError("python_dependencies 非法")
    permissions = obj.get("permissions", [])
    if not isinstance(permissions, list) or not all(isinstance(x, str) and 0 < len(x) <= 100 for x in permissions):
        raise ManifestError("permissions 非法")
    tools = obj["tools"]
    if not isinstance(tools, list) or len(tools) > 100:
        raise ManifestError("tools 非法")
    names = set()
    for i, tool in enumerate(tools):
        parsed = _validate_tool(tool, i)
        if parsed["name"] in names:
            raise ManifestError("工具名称重复")
        names.add(parsed["name"])
    skills = obj.get("skills", [])
    if not isinstance(skills, list) or len(skills) > 100:
        raise ManifestError("skills 必须是有限声明列表")
    skill_names = set()
    for skill in skills:
        declared = _strict_dict(skill, {"name", "description", "path"}, "skills")
        if not isinstance(declared.get("name"), str) or not declared["name"] or declared["name"] in skill_names:
            raise ManifestError("skills 名称非法或重复")
        skill_names.add(declared["name"])
        skill_path = validate_relpath(declared.get("path"), field="skills.path")
        if not skill_path.endswith(".md") or skill_path not in files:
            raise ManifestError("skills.path 必须指向已哈希的 .md 文件")
        if "description" in declared and not isinstance(declared["description"], str):
            raise ManifestError("skills.description 非法")
    mcp_items = obj.get("mcp", [])
    if not isinstance(mcp_items, list) or len(mcp_items) > 30:
        raise ManifestError("mcp 必须是有限声明列表")
    for item in mcp_items:
        declared = _strict_dict(item, {"name", "transport", "entrypoint", "url"}, "mcp")
        if not isinstance(declared.get("name"), str) or not declared["name"]:
            raise ManifestError("mcp.name 非法")
        if declared.get("transport") == "stdio":
            if "url" in declared or validate_relpath(declared.get("entrypoint"), field="mcp.entrypoint") not in files:
                raise ManifestError("mcp stdio 必须声明包内已哈希入口")
        elif declared.get("transport") == "http":
            from urllib.parse import urlsplit
            if "entrypoint" in declared or not isinstance(declared.get("url"), str):
                raise ManifestError("mcp http 必须声明 url")
            url = urlsplit(declared["url"])
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ManifestError("mcp url 必须是无凭据/参数的 HTTP(S) URL")
        else:
            raise ManifestError("mcp.transport 不支持")
    if 'ui' in obj:
        validate_ui(obj['ui'], tools)
    if 'config_schema' in obj:config_form(obj['config_schema'])
    signature = obj.get("signature")
    if signature is not None:
        sig = _strict_dict(signature, _ALLOWED_SIG, "signature")
        if sig.get("algorithm") != "ed25519" or not isinstance(sig.get("key_id"), str) or not isinstance(sig.get("signature"), str):
            raise ManifestError("signature 仅支持 ed25519/key_id/signature")
    return obj


def signature_status(manifest: dict[str, Any], verifier: Callable[[dict[str, Any]], bool] | None = None) -> str:
    sig = manifest.get("signature")
    if not sig:
        return "absent"
    if verifier is None:
        return "present_unverified"
    try:
        return "verified_trusted" if bool(verifier(copy.deepcopy(manifest))) else "invalid"
    except Exception:
        return "invalid"
