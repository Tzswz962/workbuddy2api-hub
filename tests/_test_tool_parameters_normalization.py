"""Regression tests for function parameters schema normalization (code 11129)
and tool_calls arguments serialization (code 11101).

Reported symptom:
Clients (e.g. Hermes Agent, OpenAI-compatible SDKs) defining zero-argument tools
emit `"parameters": {}` (missing `type: "object"`). The upstream Go struct validator
strictly rejects the request with HTTP 400:
    upstream 400: {"code":11129,"msg":"invalid function call parameters"}
    extError: {"code":"invalid_function_parameters", ...}
    displayMsg: {"zh":"工具定义不合规，请新建任务重试"}

Similar failures occur when:
  - parameters is empty dict or None
  - parameters specifies properties but omits `type: "object"`
  - required is None or a non-list string
  - history tool_calls function arguments is a dict instead of string

These tests verify:
  1. Parameters dictionary is normalized to have `type: "object"` and a valid `properties` dict.
  2. `required` field is validated into a clean list of strings or dropped.
  3. Empty `tools` array is stripped to avoid upstream rejecting empty tool list.
  4. Root-level legacy `functions` parameter is mapped to `tools`.
  5. In-history `tool_calls[].function.arguments` as a dict or None is properly serialized to JSON string.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402
_TMP = isolated_data_dirs("wb-tool-params-norm-")

import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] Zero-argument tool parameters: {} gain type: object and properties: {}")
fn = {"name": "get_time", "description": "time", "parameters": {}}
P._normalize_tool_parameters(fn)
check("type is object", fn["parameters"].get("type") == "object", fn["parameters"])
check("properties is dict", isinstance(fn["parameters"].get("properties"), dict), fn["parameters"])

print()
print("[2] Missing type with properties gains type: object")
fn = {"name": "search", "parameters": {"properties": {"q": {"type": "string"}}}}
P._normalize_tool_parameters(fn)
check("type added", fn["parameters"].get("type") == "object", fn["parameters"])
check("properties preserved", "q" in fn["parameters"].get("properties", {}), fn["parameters"])

print()
print("[3] None or non-dict parameters normalized to valid object schema")
for val in (None, [], "str", True):
    fn = {"name": "test", "parameters": val}
    P._normalize_tool_parameters(fn)
    check("val=%r -> valid schema" % (val,),
          fn["parameters"] == {"type": "object", "properties": {}}, fn["parameters"])

print()
print("[4] required field cleaned up")
fn = {"name": "t1", "parameters": {"type": "object", "properties": {"a": {}}, "required": None}}
P._normalize_tool_parameters(fn)
check("required None dropped", "required" not in fn["parameters"], fn["parameters"])

fn = {"name": "t2", "parameters": {"type": "object", "properties": {"a": {}}, "required": "a"}}
P._normalize_tool_parameters(fn)
check("required string wrapped into list", fn["parameters"].get("required") == ["a"], fn["parameters"])

fn = {"name": "t3", "parameters": {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a", None, "b"]}}
P._normalize_tool_parameters(fn)
check("required list filtered None", fn["parameters"].get("required") == ["a", "b"], fn["parameters"])

print()
print("[5] normalize_tools end-to-end on payload")
obj = {
    "tools": [
        {"type": "function", "function": {"name": "fn1", "parameters": {}}},
        {"type": "function", "name": "fn2", "parameters": {}}
    ]
}
P.normalize_tools(obj)
check("fn1 normalized", obj["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)
check("fn2 wrapped & normalized", obj["tools"][1]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)

print()
print("[6] Empty tools list stripped")
obj = {"tools": []}
P.normalize_tools(obj)
check("empty tools popped", "tools" not in obj, obj)

print()
print("[7] Legacy functions mapped to tools")
obj = {
    "functions": [
        {"name": "legacy_fn", "parameters": {}}
    ]
}
P.normalize_tools(obj)
check("functions popped", "functions" not in obj, obj)
check("tools gained", "tools" in obj and len(obj["tools"]) == 1, obj)
check("legacy_fn parameters normalized", obj["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)

print()
print("[8] sanitize_tool_calls: non-string arguments (dict, list, int, bool, None) serialized to string")
tool_calls = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "fn1", "arguments": {"city": "Beijing"}}
    },
    {
        "id": "call_2",
        "type": "function",
        "function": {"name": "fn2", "arguments": None}
    },
    {
        "id": "call_3",
        "type": "function",
        "function": {"name": "fn3", "arguments": "{\"city\":\"Shanghai\"}"}
    },
    {
        "id": "call_4",
        "type": "function",
        "function": {"name": "fn4", "arguments": [1, 2, "a"]}
    },
    {
        "id": "call_5",
        "type": "function",
        "function": {"name": "fn5", "arguments": 123}
    },
    {
        "id": "call_6",
        "type": "function",
        "function": {"name": "fn6", "arguments": True}
    }
]
res = P.sanitize_tool_calls(tool_calls)
check("dict arguments converted to JSON string", res[0]["function"]["arguments"] == '{"city": "Beijing"}', res[0]["function"]["arguments"])
check("None arguments converted to empty JSON object", res[1]["function"]["arguments"] == "{}", res[1]["function"]["arguments"])
check("string arguments untouched", res[2]["function"]["arguments"] == '{"city":"Shanghai"}', res[2]["function"]["arguments"])
check("list arguments converted to JSON string", res[3]["function"]["arguments"] == '[1, 2, "a"]', res[3]["function"]["arguments"])
check("int arguments converted to JSON string", res[4]["function"]["arguments"] == '123', res[4]["function"]["arguments"])
check("bool arguments converted to JSON string", res[5]["function"]["arguments"] == 'true', res[5]["function"]["arguments"])

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
