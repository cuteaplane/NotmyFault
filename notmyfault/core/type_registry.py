"""从插件清单构建数据类型快照。"""

from __future__ import annotations

import copy

from notmyfault.core.data_types import (
    BUILTIN_TYPES,
    TYPE_LABELS,
    DataTypeError,
    is_custom_type,
    normalize_type,
    normalize_value,
)


class TypeRegistry:
    def __init__(self, definitions=None):
        self._definitions = copy.deepcopy(definitions or {})
        for identity, definition in self._definitions.items():
            if not is_custom_type(identity):
                raise DataTypeError("自定义类型必须使用完整包名、类型名和版本", code="invalid_type")
            if definition.get("binding", "private") not in ("private", "shared"):
                raise DataTypeError("类型 binding 必须为 private 或 shared", code="invalid_type")
            definition.setdefault("binding", "private")
            definition.setdefault("available", True)
            definition["schema"] = normalize_type(definition.get("schema", "any"))
        self._check_dependencies()

    @classmethod
    def from_plugins(cls, *catalogs, include_disabled=False):
        definitions = {}
        for catalog in catalogs:
            metas = catalog.values() if isinstance(catalog, dict) else catalog
            for meta in metas:
                if not isinstance(meta, dict):
                    continue
                contributes = meta.get("contributes")
                if not isinstance(contributes, dict):
                    continue
                for declaration in contributes.get("data_types", []):
                    identity = f"{meta.get('package_name', '')}/{declaration.get('id', '')}@{declaration.get('version', '')}"
                    available = meta.get("enabled", True) is not False and not meta.get("_error")
                    if not available and not include_disabled:
                        continue
                    if declaration.get("binding") == "shared" and "schema" not in declaration:
                        raise DataTypeError(f"共享类型缺少 schema: {identity}", code="invalid_type")
                    definition = {
                        "binding": declaration.get("binding", "private"),
                        "schema": normalize_type(declaration.get("schema", "any")),
                        "label": declaration.get("label", declaration.get("id", "")),
                        "available": bool(available),
                        "plugin_id": meta.get("id", ""),
                    }
                    previous = definitions.get(identity)
                    if previous is not None:
                        if any(previous[key] != definition[key] for key in ("binding", "schema")):
                            raise DataTypeError(f"同一类型存在不同定义: {identity}", code="duplicate_type")
                        previous["available"] = previous["available"] or definition["available"]
                    else:
                        definitions[identity] = definition
        return cls(definitions)

    def _check_dependencies(self):
        def references(schema):
            if is_custom_type(schema["type"]):
                yield schema["type"]
            for key in ("items", "additional_properties"):
                if isinstance(schema.get(key), dict):
                    yield from references(schema[key])
            for child in schema.get("properties", {}).values():
                yield from references(child)
            for child in schema.get("variants", []):
                yield from references(child)

        for identity, definition in self._definitions.items():
            for dependency in references(definition["schema"]):
                target = self._definitions.get(dependency)
                if target is None:
                    definition["available"] = False
                    definition["error"] = f"缺少数据类型依赖: {dependency}"
                elif definition["binding"] == "shared" and target["binding"] == "private":
                    raise DataTypeError(f"共享类型不能包含私有类型: {identity} → {dependency}", code="private_plugin_data")
        changed = True
        while changed:
            changed = False
            for definition in self._definitions.values():
                if not definition["available"]:
                    continue
                for dependency in references(definition["schema"]):
                    if not self._definitions[dependency]["available"]:
                        definition["available"] = False
                        definition["error"] = f"数据类型依赖不可用: {dependency}"
                        changed = True
                        break

    def definition(self, identity):
        return copy.deepcopy(self._definitions.get(identity))

    def require_shared(self, declaration):
        schema = normalize_type(declaration)
        kind = schema["type"]
        if is_custom_type(kind):
            definition = self._definitions.get(kind)
            if definition is None:
                raise DataTypeError(f"数据类型未安装或版本不匹配: {kind}", code="unknown_type")
            if not definition["available"]:
                raise DataTypeError(f"数据类型不可用: {kind}", code="unavailable_type")
            if definition["binding"] != "shared":
                raise DataTypeError("插件私有数据不能参与变量传递", code="private_plugin_data")
        for key in ("items", "additional_properties"):
            if isinstance(schema.get(key), dict):
                self.require_shared(schema[key])
        for child in [*schema.get("properties", {}).values(), *schema.get("variants", [])]:
            self.require_shared(child)

    def binding_value(self, value):
        if isinstance(value, list):
            return [self.binding_value(item) for item in value]
        if isinstance(value, dict):
            if is_custom_type(value.get("$type")):
                self.require_shared(value["$type"])
                value = normalize_value(value, value["$type"], self)
            return {key: self.binding_value(item) for key, item in value.items()}
        return value

    def snapshot(self):
        return copy.deepcopy(self._definitions)

    def make_value(self, identity, data, summary=""):
        value = {"$type": identity, "data": data}
        if summary:
            value["summary"] = summary
        return normalize_value(value, identity, self)

    def catalog(self):
        builtins = []
        for kind in sorted(BUILTIN_TYPES):
            if kind == "union":
                declaration = {"type": "union", "variants": ["text", "null"]}
            elif kind == "timestamp":
                declaration = {"type": "timestamp", "unit": "seconds"}
            else:
                declaration = kind
            builtins.append({"id": kind, "label": TYPE_LABELS[kind], "schema": normalize_type(declaration)})
        return {"builtins": builtins, "custom": self.snapshot()}
