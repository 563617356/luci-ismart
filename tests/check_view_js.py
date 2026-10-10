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

# ------------------------------------------------------------------
# 4. LuCI 全局符号使用 vs 'require' 声明
#
# 同类问题：用了 rpc / uci / form 等注入的全局符号，但忘了写对应的
# 'require xxx'，真机上表现为 "rpc is not defined"。
# node --check 同样抓不到——未声明的标识符在语法上完全合法。
#
# 注意必须抓「含点的调用」本身（rpc.call(...) 里的 rpc），
# 光匹配 \b(\w+)\s*\( 抓到的是 call 不是 rpc。
# ------------------------------------------------------------------

# 'require <mod>' 会注入的全局 -> 模块名
NEEDS_REQUIRE = {
    "view": "view", "uci": "uci", "fs": "fs", "dom": "dom",
    "ui": "ui", "poll": "poll", "rpc": "rpc", "form": "form",
    "network": "network", "firewall": "firewall", "system": "system",
    "cgi": "cgi", "token": "token", "auth": "auth",
}

# 无需 require 就存在的全局
FREE = {
    "E", "D", "L", "_", "p", "require", "document", "window", "console",
    "Promise", "Array", "Object", "String", "Number", "Boolean", "JSON",
    "Math", "Error", "RegExp", "Date", "parseInt", "parseFloat", "isNaN",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "location", "navigator", "fetch", "Map", "Set", "Symbol",
    # 本文件自己定义的
    "runCtl", "parseJson", "injectStyle", "CSS_TEXT",
}

declared = set()
for m in re.finditer(r"^'require\s+([\w./-]+)'", src, re.M):
    declared.add(m.group(1).split("/")[-1].split(".")[-1])

# 剥注释时保留行号：把块注释替换成等量换行，行数不变，
# 这样报出来的行号仍指向原文件，用户能直接跳过去。
def strip_comments_keep_lines(text):
    def repl(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    t = re.sub(r"/\*.*?\*/", repl, text, flags=re.S)
    t = re.sub(r"^[ \t]*//.*$", lambda m: " " * len(m.group(0)), t, flags=re.M)
    return t


nocomment = strip_comments_keep_lines(src)

# 符号扫描用的版本：再把字符串内容清空，避免中文提示里的词被当符号。
# 属性名检查（null 默认值）必须用 nocomment 本身——那里面的
# 'selected' 引号内容是关键，清空了就没法匹配。
for_sym = re.sub(r"'[^'\n]*'", "''", nocomment)
for_sym = re.sub(r'"[^"\n]*"', '""', for_sym)

# 抓「对象.方法(...)」与「对象(...)」两种使用形式里的对象名
used = set()
for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\.\s*[A-Za-z_$][\w$]*\s*\(", for_sym):
    used.add(m.group(1))
for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", for_sym):
    used.add(m.group(1))
for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\.\s*[A-Za-z_$][\w$]", for_sym):
    used.add(m.group(1))

undeclared = sorted(
    n for n in used
    if n in NEEDS_REQUIRE and n not in declared and n not in FREE
)

print()
print("已声明的 require: %s" % " ".join(sorted(declared)))
print("用到的注入符号: %s" % " ".join(sorted(n for n in used if n in NEEDS_REQUIRE)))
if undeclared:
    print("!! 以下符号被使用但没有 'require' 声明：")
    for n in undeclared:
        print("     %s  -> 需要加 'require %s';" % (n, NEEDS_REQUIRE[n]))
    sys.exit(1)
print("ok  所有注入符号都有 require 声明（或本身是全局）")

# ------------------------------------------------------------------
# 5. E() 的属性值不能是 null
#
# LuCI 的 DOM 构造器会对属性值调 charAt 来决定如何 set，
# 传 null 进去抛 "xxx.charAt is not a function"，元素渲染失败。
# 真机上连踩两次：
#   E('option',  { 'selected': cond ? 'selected' : null })   -> opt.charAt
#   E('a',       { 'external': url   ? true       : null })  -> opt.charAt
#
# 通用形式：'<attr>': <cond> ? <anything> : null
# 值是字符串还是布尔值都一样会炸，所以两种都要匹配。
# ------------------------------------------------------------------
bad_attr = []
for m in re.finditer(
        r"['\"](\w[\w-]*)['\"]\s*:\s*[^,}\n]*\?\s*"
        r"(?:['\"][^'\"]*['\"]|true|false|[A-Za-z_$][\w$]*)\s*:\s*null",
        nocomment):
    bad_attr.append((m.group(1), nocomment[:m.start()].count("\n") + 1, "E() 属性"))

# 同样的坑也在函数参数位置：ui.addNotification(null, ...)
# 真机上栽过——这个 null 藏在 Promise 链里，表现为「点了保存但没写进去」，
# 控制台只有一条 opt.charAt，看不出跟 addNotification 有关。
for m in re.finditer(r"\bui\.addNotification\s*\(\s*null\s*,", nocomment):
    bad_attr.append(("addNotification 第 1 参",
                     nocomment[:m.start()].count("\n") + 1, "函数参数"))

if bad_attr:
    print()
    print("!! 以下位置把 null 传给了 LuCI，渲染/保存时会抛 charAt 错误：")
    for name, ln, kind in bad_attr:
        print("     %s 在第 %d 行（%s）——改成一个具体的值/字符串 id"
              % (name, ln, kind))
    sys.exit(1)
print("ok  没有把 null 作为 E() 属性值或 LuCI 通知 id")

# ------------------------------------------------------------------
# 6. uci.set 必须是四参数
#
# 这个版本的 uci.js 签名是 set(conf, sid, opt, val)，
# 第三个参数会被当 option 名，内部调 opt.charAt(0) —— 传对象就炸。
#
# 真机上栽过：写成 uci.set('ismart','main',map) 后，点保存毫无反应，
# 配置一点没变，而堆栈里只有一句 opt.charAt is not a function，
# 跟调用点隔着两层，很难反应过来是参数个数不对。
# ------------------------------------------------------------------
bad_set = []
for m in re.finditer(r"\buci\.set\s*\(([^)]*)\)", nocomment):
    inner = m.group(1)
    # 按顶层逗号数参数个数（本文件里没有嵌套逗号的复杂表达式）
    if len(inner.split(",")) == 3 and "uci.set" in m.group(0):
        bad_set.append(nocomment[:m.start()].count("\n") + 1)
    elif len(inner.split(",")) == 2:
        bad_set.append(nocomment[:m.start()].count("\n") + 1)

if bad_set:
    print()
    print("!! uci.set 参数个数不对（应为 4 个：conf, section, option, value）：")
    for ln in bad_set:
        print("     第 %d 行" % ln)
    print("     传对象或缺 value 会让内部把 map 当 option 名，抛 charAt 错误")
    sys.exit(1)
print("ok  uci.set 都是四参数形式")
