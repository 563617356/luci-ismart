#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查：视图里 this.xxx() 的每个 xxx 都必须在本视图里定义过。

node --check 抓不到这类错误——调用不存在的方法在语法上完全合法，
只有真机运行时才炸（"this.renderValue is not a function"），
而且 LuCI 会给出整页白屏。

真机上就是这么发现的：v1.1.0 的界面里 renderValue / renderListValue
被调用了 8 次，但从没定义过。CI 当时一路绿灯。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "..", "htdocs",
                 "luci-static", "resources", "view", "ismart", "ismart.js")

src = io.open(P, encoding="utf-8").read()
lines = src.split("\n")

# 1. 本视图定义的方法（含各级缩进的 "name: function"）
defined = set()
for ln in lines:
    m = re.match(r"^\t+(\w+):\s*function", ln)
    if m:
        defined.add(m.group(1))
# 文件末尾的顶层 function xxx（辅助函数）
for m in re.finditer(r"^function\s+(\w+)", src, re.M):
    defined.add(m.group(1))

# 2. 本视图里所有 this.xxx( 的调用
called = {}
for i, ln in enumerate(lines, 1):
    for m in re.finditer(r"\bthis\.(\w+)\s*\(", ln):
        called.setdefault(m.group(1), []).append(i)

# 3. LuCI view 基类会注入的方法，不算缺失
BASE = {
    "load", "render", "handleSave", "handleSaveApply", "handleReset",
    "handleResetAdvance", "ready", "poll", "pollStatus",
    "replaceClass", "addClass", "removeClass", "title",
}

print("定义的方法 (%d):" % len(defined))
for n in sorted(defined):
    print("   ", n)
print()

print("调用的 this.xxx (%d):" % len(called))
for n in sorted(called):
    print("   %-20s 行 %s" % (n, called[n]))
print()

missing = {n: v for n, v in called.items() if n not in defined and n not in BASE}
if missing:
    print("!! 以下方法被调用但未定义：")
    for n, v in sorted(missing.items()):
        print("     %s  (行 %s)" % (n, v))
    sys.exit(1)

print("ok  所有 this.xxx() 调用都有对应定义（或由 LuCI 基类提供）")
