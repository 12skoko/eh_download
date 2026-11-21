"""Validate JSON once per batch and build predicates for EH listing metadata."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime

import regex

from .models import Manga

Predicate = Callable[[Manga], bool]
# Frozen v2 migration target, also used by a default CrawlConfig.
V2_ALGORITHM = (
    '{"op":"all","children":['
    '{"field":"category","op":"exact","values":["Manga","Doujinshi"]},'
    '{"op":"any","children":['
    '{"field":"tag_text","op":"contains","values":["chinese"]},'
    '{"field":"rating","op":"gte","value":3}]}]}'
)
TEXT_FIELDS = {
    "title": "name",
    "group_title": "real_name",
    "uploader": "uploader",
    "tag_text": "tags_raw",
}
FIELD_ATTRS = {
    **TEXT_FIELDS, "tags": "tags_raw", "category": "category",
    "pages": "pages", "rating": "rating", "posted_at": "posted_at",
}
TEXT_OPS = {"contains", "word", "equals", "starts", "ends", "regex"}
EMPTY_OPS = {"empty", "present"}


def normalized_eh_tags(raw: str) -> frozenset[str]:
    result = set()
    namespace = ""
    for token in raw.casefold().split(","):
        if ":" in token:
            namespace, token = token.split(":", 1)
            namespace = namespace.strip()
        if namespace and token.strip():
            result.add(f"{namespace}:{token.strip()}")
    return frozenset(result)


class RuleTrace:
    """One preview evaluation at a time; capture only predicates actually called."""

    def __init__(self):
        self.current = {}
        self.fields = {}

    def observe(self, predicate: Predicate, field: str | None = None) -> Predicate:
        def traced(row):
            parent = self.current
            step = {}
            parent.setdefault("children", []).append(step)
            self.current = step
            try:
                matched = predicate(row)
                step["matched"] = matched
                if field and field not in self.fields:
                    actual = getattr(row, FIELD_ATTRS[field])
                    if actual is not None and field == "posted_at":
                        actual = utc_date(row).isoformat()
                    if actual is not None and field == "rating":
                        actual /= 10
                    self.fields[field] = actual
                return matched
            finally:
                self.current = parent

        return traced

    def indexed(self, results):
        for index, result in enumerate(results):
            self.current["last"] = index
            if hasattr(result, "span") and "span" not in self.current:
                self.current["span"] = result.span()
            yield result

    def take(self):
        result = {"routes": self.current["children"], "fields": self.fields}
        self.current = {}
        self.fields = {}
        return result


def compile_rule(
    raw: str, label: str, *, today: date | None = None, trace: RuleTrace | None = None
) -> Predicate:
    today = today or datetime.now(UTC).date()
    if len(raw) > 256_000:
        raise ValueError(f"{label} 超过长度限制")
    try:
        root = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{label} JSON 无效：{exc}") from None
    # Module and panel notes can exist even when this path is disabled.
    if isinstance(root, dict) and "rule" in root:
        notes = root.get("notes")
        if (set(root) != {"rule", "notes"} or not isinstance(notes, dict)
                or notes.keys() - {"module", "include", "exclude"}
                or not all(isinstance(value, str) for value in notes.values())):
            raise ValueError(f"{label}：模块、包含条件和排除条件的备注须为文本")
        root = root["rule"]
    if root is None:
        def disabled(row):
            return False

        return trace.observe(disabled) if trace is not None else disabled
    count = 0

    def build(node, path, depth):
        predicate = compile_node(node, path, depth)
        return trace.observe(predicate, node.get("field")) if trace is not None else predicate

    def compile_node(node, path, depth):
        nonlocal count
        count += 1

        def require(condition, message):
            if not condition:
                raise ValueError(f"{path}：{message}")

        require(count <= 100 and depth <= 8, "每棵树最多 100 个节点、8 层嵌套")
        require(isinstance(node, dict), "需要条件对象")
        if "note" in node:
            require(isinstance(node["note"], str), "备注须为文本")
            node = {key: value for key, value in node.items() if key != "note"}
        op = node.get("op")
        require(isinstance(op, str), "缺少匹配方式")
        if op == "always":
            require(set(node) == {"op"}, "匹配全部不接受其他参数")
            return lambda row: True
        if op in {"all", "any", "not"}:
            children = node.get("children")
            require(
                set(node) == {"op", "children"} and isinstance(children, list) and children,
                "条件组必须包含子项",
            )
            require(op != "not" or len(children) == 1, "取反组仅接受一个子项")
            parts = [
                build(child, f"{path}.children[{i}]", depth + 1) for i, child in enumerate(children)
            ]
            if op == "not":
                return lambda row: not parts[0](row)
            combine = all if op == "all" else any
            return lambda row: combine(part(row) for part in parts)

        field = node.get("field")
        require(isinstance(field, str), "缺少筛选字段")
        attr = FIELD_ATTRS.get(field)
        require(attr is not None, "未知筛选字段")
        if op in EMPTY_OPS:
            require(set(node) == {"field", "op"}, "空值判断不接受其他参数")
            if field == "tags":
                return lambda row: bool(normalized_eh_tags(row.tags_raw)) == (op == "present")
            return lambda row: (getattr(row, attr) in (None, "")) == (op == "empty")

        if field in TEXT_FIELDS or field in {"tags", "category"}:
            return _compile_text(node, field, attr, op, path, require, trace)
        return _compile_range(node, field, attr, op, path, require, today)

    return build(root, label, 0)


def _compile_text(node, field, attr, op, path, require, trace):
    allowed = {"field", "op", "values", "match"}
    if field in TEXT_FIELDS:
        allowed.add("case")
    require(not (set(node) - allowed), "未知条件属性")
    values = node.get("values")
    require(
        isinstance(values, list)
        and 0 < len(values) <= 1000
        and all(isinstance(v, str) and v.strip() and len(v) <= 2000 for v in values),
        "需要 1～1000 个非空文本值，每项最长 2000 字符",
    )
    require(node.get("match", "any") in ("any", "all"), "多值关系必须为 any 或 all")
    aggregate = all if node.get("match") == "all" else any
    combine = aggregate if trace is None else lambda hits: aggregate(trace.indexed(hits))
    if op == "none" or field == "category":
        require(node.get("match", "any") == "any", "本条件仅支持任一值")
    if field == "tags":
        require(op in {"exact", "none"}, "未知标签匹配方式")
        require(
            all(re.fullmatch(r"[a-z][a-z0-9_]*:[^,:]+", v.strip(), re.IGNORECASE) for v in values),
            "需要 namespace:name 标签",
        )
        wanted = []
        for value in values:
            namespace, name = value.strip().casefold().split(":", 1)
            require(name.strip(), "标签名称不能为空")
            wanted.append(f"{namespace}:{name.strip()}")

        def tags(row):
            actual = normalized_eh_tags(row.tags_raw)
            matched = combine(v in actual for v in wanted)
            return not matched if op == "none" else matched

        return tags
    if field == "category":
        require(op in {"exact", "none"}, "未知分类匹配方式")
        return lambda row: combine(row.category == v for v in values) == (op == "exact")
    require(op in TEXT_OPS and type(node.get("case", False)) is bool, "文本匹配方式无效")
    sensitive = node.get("case", False)
    if op == "regex":
        try:
            patterns = [regex.compile(v, 0 if sensitive else regex.I) for v in values]
        except (regex.error, OverflowError, RecursionError) as exc:
            raise ValueError(f"{path}：正则错误 {exc}") from None

        def match_regex(row):
            try:
                return combine(p.search(getattr(row, attr), timeout=0.1) for p in patterns)
            except TimeoutError:
                raise ValueError(f"{path}：正则匹配超时（{row.manga_id}）") from None

        return match_regex
    needles = values if sensitive else [v.lower() for v in values]
    if op == "word":
        words = [re.compile(r"\b" + re.escape(v) + r"\b") for v in needles]

        def word(row):
            raw = getattr(row, attr)
            value = raw if sensitive else raw.lower()
            matched = combine(p.search(value) for p in words)
            # Unicode lowercasing can change lengths. Only mark offsets in the original text.
            if trace is not None and len(value) != len(raw):
                trace.current.pop("span", None)
            return matched

        return word
    compare = {
        "equals": str.__eq__,
        "contains": str.__contains__,
        "starts": str.startswith,
        "ends": str.endswith,
    }[op]

    def match_text(row):
        raw = getattr(row, attr)
        value = raw if sensitive else raw.lower()
        matched = combine(compare(value, needle) for needle in needles)
        if trace is not None and matched and len(raw) == len(value):
            needle = needles[0 if aggregate is all else trace.current["last"]]
            start = len(value) - len(needle) if op == "ends" else value.find(needle)
            trace.current["span"] = (start, start + len(needle))
        return matched

    return match_text


def _compile_range(node, field, attr, op, path, require, today):
    require(set(node) == {"field", "op", "value"}, "需要一个数值、日期或区间")
    value = node["value"]
    if op == "recent":
        require(field == "posted_at" and type(value) is int and value >= 1, "最近天数须为正整数")
        return lambda row: row.posted_at is not None and 0 <= (today - utc_date(row)).days < value
    require(op in {"eq", "gte", "lte", "between"}, "未知范围判断")
    bounds = value if op == "between" else [value]
    require(
        isinstance(bounds, list) and len(bounds) == (2 if op == "between" else 1), "区间需要上下限"
    )
    if field == "posted_at":
        try:
            bounds = [date.fromisoformat(v) for v in bounds]
        except (ValueError, TypeError):
            raise ValueError(f"{path}：日期格式应为 YYYY-MM-DD") from None
    else:
        for bound in bounds:
            valid = type(bound) is int and bound >= 0
            if field == "rating":
                valid = type(bound) in (float, int) and 0 <= bound <= 5
            require(valid, "页数须为非负整数，星级须在 0～5 之间")
    require(bounds[0] <= bounds[-1], "下限超过上限")

    def match_range(row):
        actual = getattr(row, attr)
        if actual is None:
            return False
        if field == "posted_at":
            actual = utc_date(row)
        elif field == "rating":
            actual /= 10
        if op == "gte":
            return actual >= bounds[0]
        if op == "lte":
            return actual <= bounds[0]
        return bounds[0] <= actual <= bounds[-1]

    return match_range


def utc_date(row):
    value = row.posted_at
    return value.astimezone(UTC).date() if value.tzinfo else value.date()
