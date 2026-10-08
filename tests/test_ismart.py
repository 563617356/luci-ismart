#!/usr/bin/env python3
"""luci-ismart 核心逻辑测试。

为什么要 mock uci
    切换逻辑的核心是对 /etc/config/{network,dhcp,firewall} 做一连串
    uci 读写。这些在 PC 上跑不起来，但恰恰是最需要验证的部分——
    写错一个 option 名，设备上就是「编译安装全成功、功能全无」。

    所以这里实现一个最小 uci 替身，把配置读成
    {段: {选项: 值}} 字典，支持 get/set/add_list/delete/commit/show，
    覆盖 core.sh 用到的全部调用。于是 core.sh 的完整切换流程
    可以在本机跑完并断言结果。

为什么用常驻服务
    一次完整切换要调用几十次 uci。若每条命令都起一个 Python 解释器，
    整套测试会从秒级掉到分钟级（实测单个用例 30~100 秒）。
    这里用一个常驻进程持有 mock 状态，shim 通过本机 TCP 与它通信。

状态唯一性
    mock 状态只存在于常驻服务里。测试代码若直接改自己那份副本，
    会与服务侧状态不一致，产生莫名其妙的断言失败。
    所以修改配置一律走 Env.set()，查询一律走 Env.value()。

运行：python3 tests/test_ismart.py
"""

import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SELF = os.path.abspath(__file__)
CORE = os.path.join(ROOT, 'root', 'usr', 'libexec', 'ismart', 'core.sh')


# --------------------------------------------------------------------- mock uci

class ListVal(list):
    """带「这是 list 值」标记的列表。

    真实 uci 里 `list network 'lan'` 即使只有一个元素，写回时仍然是
    `list network 'lan'`，不会退化成 `option`。这一点很关键：
    切换流程「cp 备份回原位 + uci commit」之后要能逐字节比对，
    mock 一旦把单值 list 写成 option，恢复校验就会假报不一致。

    而普通的 list 容器无法区分「list 值」与「多值累积的标量」，
    所以这里用一个子类显式携带标记。
    """


class FakeUci:
    """把 /etc/config 下的配置文件读成 {段: {选项: 值|列表}}。

    值的表示：
        标量        -> str
        多值(list)  -> ['a', 'b']
    匿名段用 '@type[i]' 寻址，与真实 uci 一致。
    """

    def __init__(self, conf_dir):
        self.conf_dir = conf_dir
        self.data = {}
        # 每个包最后一次「已知文件内容」的签名。
        # 真实 uci 每次调用都是新进程，一律从文件读；而这里的常驻实例
        # 持有内存态，不主动同步就会与磁盘脱节。
        self._sig = {}
        # 每个包原始文件末尾的换行，save 时还原
        self._trail = {}

    def _file_sig(self, pkg):
        path = os.path.join(self.conf_dir, pkg)
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError:
            return None

    def _sync_from_disk(self, pkg):
        """若磁盘文件被外部改过（典型情况：脚本用 cp 恢复了备份），
        就丢弃内存态重新加载。

        没有这一步会出很难查的错：ismart_apply_router 的流程是
        「cp 备份回原位 + uci commit」。常驻实例仍持着旁路由态的内存数据，
        commit 会把刚恢复好的文件又覆盖回去，表现为「恢复无效」，
        而所有单条命令的返回值都正常（rc=0）。
        """
        sig = self._file_sig(pkg)
        if pkg in self.data and self._sig.get(pkg) != sig:
            del self.data[pkg]
            # 尾部换行风格也随文件变化，cp 恢复的备份可能与旁路由态不同
            self._trail.pop(pkg, None)
        return sig

    def _mark(self, pkg):
        self._sig[pkg] = self._file_sig(pkg)

    # ---- 段名解析：'@zone[0]' -> 类型 zone 的第 0 个匿名段 ----
    def _resolve_section(self, pkg, sec):
        d = self.data.setdefault(pkg, {})
        if sec in d:
            return sec
        m = re.fullmatch(r'@(\w+)\[(\d+)\]', sec)
        if not m:
            return None
        stype, idx = m.group(1), int(m.group(2))
        n = 0
        for name, body in d.items():
            if body.get('__type__') == stype:
                if n == idx:
                    return name
                n += 1
        return None

    @staticmethod
    def _split(path):
        """'network.lan.ipaddr' -> (pkg, sec, opt)"""
        parts = path.split('.')
        if len(parts) == 2:
            return parts[0], parts[1], None
        return parts[0], parts[1], '.'.join(parts[2:])

    # ---- 解析 UCI 配置文件文本 ----
    def load(self, pkg):
        if pkg in self.data:
            return
        sig = self._file_sig(pkg)
        path = os.path.join(self.conf_dir, pkg)
        # 记住原始文件末尾的换行，save 时照原样还原
        if isinstance(sig, bytes):
            m = re.search(rb'(\n*)$', sig)
            self._trail.setdefault(pkg, m.group(1).decode('utf-8'))
        d = {}
        if os.path.exists(path):
            anon = {}
            cur = None
            with open(path, encoding='utf-8', errors='replace') as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('#'):
                        continue

                    m = re.match(r"config\s+(\S+)(?:\s+'([^']*)')?", line)
                    if m:
                        stype, name = m.group(1), m.group(2)
                        if name:
                            cur = name
                        else:
                            n = anon.get(stype, 0)
                            anon[stype] = n + 1
                            cur = '@%s[%d]' % (stype, n)
                        d[cur] = {'__type__': stype}
                        continue

                    if cur is None:
                        continue

                    # UCI 语法有两种形式，不能只按 '=' 切：
                    #   option key 'value'    标量
                    #   list   key 'value'    多值（可重复出现）
                    #   key=value             set 命令写出的形式
                    if '=' in line and not re.match(r'^(option|list)\s', line):
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        is_list = False
                    else:
                        m2 = re.match(r"^(option|list)\s+(\S+)\s*(.*)$", line)
                        if not m2:
                            continue
                        is_list = (m2.group(1) == 'list')
                        k = m2.group(2)
                        v = m2.group(3).strip().strip("'\"")

                    if not k:
                        continue

                    if is_list:
                        # list 值一律用 ListVal，保留 list 属性，
                        # 哪怕只有一个元素也不能退化成标量
                        if not isinstance(d[cur].get(k), ListVal):
                            old = d[cur].get(k)
                            d[cur][k] = ListVal([old] if old is not None else [])
                        d[cur][k].append(v)
                    elif k in d[cur] and isinstance(d[cur][k], ListVal):
                        # 同一 key 先 list 后 option：只保留最后一个
                        d[cur][k] = v
                    elif k in d[cur]:
                        # 同一 key 出现两次 option：多值累积
                        d[cur][k] = [d[cur][k], v]
                    else:
                        d[cur][k] = v
        self.data[pkg] = d
        self._sig[pkg] = sig

    def save(self, pkg):
        """写回配置文件。

        用 UCI 的标准空格语法而非 set 命令的 k=v 形式——
        虽然本解析器两种都吃，但产物要像真正的 OpenWrt 配置，便于人工排查。

        结尾的空行数记录在 self._trail 里：原始配置末尾可能没有换行，
        若每次 save 都补一个，恢复校验就会因为多出的空行报假不一致。
        """
        d = self.data.get(pkg, {})
        out = []
        for name, body in d.items():
            if name.startswith('@'):
                out.append("config %s" % body.get('__type__', 'unknown'))
            else:
                out.append("config %s '%s'" % (body.get('__type__', 'unknown'), name))
            for k, v in body.items():
                if k == '__type__':
                    continue
                if isinstance(v, list):
                    for item in v:
                        out.append("\tlist %s '%s'" % (k, item))
                else:
                    out.append("\toption %s '%s'" % (k, v))
            out.append('')
        # out 的每个元素已经自带换行，join 之后末尾必然多一个空行，
        # 这正好对应原文件里段与段之间的空行；再去掉最后一个空元素，
        # 剩下的结尾交给 _trail 按原始风格还原。
        # 这里若再多补一个 '\n'，原文件只有单个换行时就会多出一行。
        text = '\n'.join(out[:-1]) if len(out) > 1 else ''
        text += self._trail.get(pkg, '\n')
        with open(os.path.join(self.conf_dir, pkg), 'w',
                  encoding='utf-8', newline='\n') as f:
            f.write(text)

    # ---- 命令实现 ----
    def cmd(self, argv):
        args = [a for a in argv if a != '-q']
        if not args:
            return 1
        op = args[0]

        # 每条命令前先与磁盘对账。常驻实例不像真实 uci 那样每次重读文件，
        # 少了这一步，「cp 恢复备份 + uci commit」会把内存态写回去。
        if op in ('get', 'set', 'add_list', 'delete', 'commit', 'show'):
            pkgs = []
            if op == 'commit':
                pkgs = args[1:] or list(self.data.keys())
            elif len(args) > 1 and '.' in args[1]:
                pkgs = [self._split(args[1])[0]]
            for p in pkgs:
                self._sync_from_disk(p)

        if op == 'get':
            pkg, sec, opt = self._split(args[1])
            self.load(pkg)
            real = self._resolve_section(pkg, sec)
            if real is None:
                return 1
            body = self.data[pkg][real]
            if opt is None:
                print(real)
                return 0
            # 支持 opt.0 形式的 list 取值
            m = re.fullmatch(r'(.+)\.(\d+)$', opt)
            key, idx = (m.group(1), int(m.group(2))) if m else (opt, None)
            if key not in body:
                return 1
            val = body[key]
            if idx is not None:
                if not isinstance(val, list) or idx >= len(val):
                    return 1
                print(val[idx])
            else:
                print(' '.join(val) if isinstance(val, list) else val)
            return 0

        if op == 'set':
            for a in args[1:]:
                if '=' not in a:
                    continue
                k, v = a.split('=', 1)
                pkg, sec, opt = self._split(k)
                self.load(pkg)
                if opt is None:
                    # set pkg.sec=type  -> 创建/覆盖一个具名段
                    self.data[pkg][sec] = {'__type__': v}
                    continue
                real = self._resolve_section(pkg, sec)
                if real is None:
                    if sec.startswith('@'):
                        # 匿名段不存在就跳过，与真实 uci 行为一致
                        continue
                    self.data[pkg][sec] = {'__type__': 'unknown'}
                    real = sec
                m = re.fullmatch(r'(.+)\.(\d+)$', opt)
                if m and isinstance(self.data[pkg][real].get(m.group(1)), list):
                    self.data[pkg][real][m.group(1)][int(m.group(2))] = v
                else:
                    # 覆写一个原本是 list 的 key：真实 uci 会退化成标量
                    self.data[pkg][real][opt] = v
            return 0

        if op == 'add_list':
            a = args[1]
            if '=' not in a:
                return 1
            k, v = a.split('=', 1)
            pkg, sec, opt = self._split(k)
            self.load(pkg)
            real = self._resolve_section(pkg, sec)
            if real is None:
                return 1
            body = self.data[pkg][real]
            cur = body.get(opt)
            if cur is None:
                body[opt] = ListVal([v])
            elif isinstance(cur, ListVal):
                cur.append(v)
            else:
                # 原先是标量，add_list 会把它转成多值 list
                body[opt] = ListVal([cur, v])
            return 0

        if op == 'delete':
            pkg, sec, opt = self._split(args[1])
            self.load(pkg)
            real = self._resolve_section(pkg, sec)
            if real is None:
                return 1
            body = self.data[pkg][real]
            if opt is None:
                del self.data[pkg][real]
            elif opt in body:
                del body[opt]
            else:
                return 1
            return 0

        if op == 'commit':
            for p in (args[1:] or list(self.data.keys())):
                if p in self.data:
                    self.save(p)
                    # 写完立刻记录签名：否则下次对账会把这次提交
                    # 误判成「外部改动了文件」，从而丢掉刚提交的内容。
                    self._mark(p)
            return 0

        if op == 'show':
            pkg = args[1] if len(args) > 1 else None
            for p in (sorted(self.data) if pkg is None else [pkg]):
                self.load(p)
                for sec, body in self.data[p].items():
                    print('%s.%s=%s' % (p, sec, body.get('__type__', '')))
                    for k, v in body.items():
                        if k == '__type__':
                            continue
                        if isinstance(v, list):
                            print("%s.%s.%s='%s'" % (p, sec, k, "' '".join(v)))
                        else:
                            print("%s.%s.%s='%s'" % (p, sec, k, v))
            return 0

        return 1


# ---------------------------------------------------------------- 常驻 uci 服务

def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UciHandler(socketserver.StreamRequestHandler):
    """所有连接共用 server.uci 这一个实例。

    关键是状态必须跨命令存活：uci 的语义是「set 之后 get 能读到」。
    若每条命令新建实例并从文件重载，set 的结果会被立刻丢掉——
    表现就是 delete/add_list 返回成功但值纹丝不动，非常难查。

    状态在 commit 时才落盘；脚本里的修改最后都会 commit_all，
    所以测试断言读文件时看到的是最终结果。
    代价是状态不在线程间隔离，但 shim 调用本来就是串行的。
    """

    def handle(self):
        import contextlib
        import io

        u = self.server.uci
        for raw in self.rfile:
            line = raw.decode('utf-8').strip()
            if not line:
                continue
            try:
                argv = json.loads(line)
            except ValueError:
                continue
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    rc = u.cmd(argv)
                except Exception:
                    rc = 1
            resp = json.dumps({'rc': rc, 'out': buf.getvalue().strip()})
            self.wfile.write((resp + '\n').encode('utf-8'))
            self.wfile.flush()


class _UciServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port, conf_dir):
    srv = _UciServer(('127.0.0.1', port), _UciHandler)
    # 所有连接共用这一个实例，状态才能跨命令保持
    srv.uci = FakeUci(conf_dir)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ----------------------------------------------------------------- 测试脚手架

NETWORK_ROUTER = """\
config interface 'loopback'
	option device 'lo'
	option proto 'static'
	option ipaddr '127.0.0.1'

config device
	option name 'br-lan'
	option type 'bridge'
	list ports 'lan1'
	list ports 'lan2'

config interface 'lan'
	option device 'br-lan'
	option proto 'static'
	option ipaddr '192.168.1.1'
	option netmask '255.255.255.0'
	option ip6assign '60'

config interface 'wan'
	option device 'eth0'
	option proto 'dhcp'
"""

DHCP_ROUTER = """\
config dnsmasq
	option domainneeded '1'

config dhcp 'lan'
	option interface 'lan'
	option start '100'
	option limit '150'
	option leasetime '12h'
"""

FIREWALL_ROUTER = """\
config defaults
	option syn_flood '1'
	option input 'ACCEPT'
	option output 'ACCEPT'
	option forward 'REJECT'

config zone
	option name 'lan'
	list network 'lan'
	option input 'ACCEPT'
	option output 'ACCEPT'
	option forward 'ACCEPT'

config zone
	option name 'wan'
	list network 'wan'
	option input 'REJECT'
	option output 'ACCEPT'
	option forward 'REJECT'
	option masquerade '1'

config forwarding
	option src 'lan'
	option dest 'wan'
"""

ISMART_CONF = """\
config ismart 'main'
	option lan_section 'lan'
	option wan_section 'wan'
	option lan_ipaddr '192.168.1.2'
	option lan_netmask '255.255.255.0'
	option gateway '192.168.1.1'
	option dns_primary ''
	option masq '0'
	option wan_mode 'disable'
	option guard_timeout '90'
"""

FILES = {
    'network': NETWORK_ROUTER,
    'dhcp': DHCP_ROUTER,
    'firewall': FIREWALL_ROUTER,
    'ismart': ISMART_CONF,
}


class Env:
    """一次测试用的临时根目录 + 连到常驻 mock 服务的客户端。"""

    def __init__(self, files=None):
        self.root = tempfile.mkdtemp(prefix='ismart-test-')
        self.conf_dir = os.path.join(self.root, 'etc', 'config')
        os.makedirs(self.conf_dir, exist_ok=True)
        for name, content in (files or FILES).items():
            with open(os.path.join(self.conf_dir, name), 'w',
                      encoding='utf-8', newline='\n') as f:
                f.write(content)

        self._seq = 0
        self._port = free_port()
        self._server = None
        self.uci_calls = 0
        self._start_server()
        self._write_shim()

    def _write_shim(self):
        """生成 uci 转发 shim，把调用转给常驻 mock 服务。

        只在 __init__ 里写一次。sh() 里反复重写有两个问题：
        既慢，又会把外部埋的桩/计数 shim 覆盖掉。

        每次 uci 调用会起一个 Python 解释器做转发，一次完整切换约 30 次，
        在 Windows 上大约 30 秒。这个开销是本机 Python 启动慢所致，
        CI 的 Linux 上要快两个数量级，整体仍在可接受范围。
        想再快就得用纯 shell 拼 TCP 报文，那会让测试本身变得不可信——
        为了跑得快而牺牲被测逻辑的可信度是本末倒置。
        """
        shim = os.path.join(self.root, 'bin')
        os.makedirs(shim, exist_ok=True)
        p = os.path.join(shim, 'uci')
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write('#!/bin/sh\nexec "%s" "%s" --uci-client "$@"\n'
                    % (sys.executable, SELF))
        os.chmod(p, 0o755)

    # ---- 常驻服务 ----

    def _start_server(self):
        env = dict(os.environ)
        env['ISMART_TEST_PORT'] = str(self._port)
        self._server = subprocess.Popen(
            [sys.executable, SELF, '--uci-server', self.conf_dir],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        for _ in range(200):
            if self._server.poll() is not None:
                err = self._server.stderr.read().decode('utf-8', 'replace')
                raise AssertionError('uci-server 启动失败:\n%s' % err)
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect(('127.0.0.1', self._port))
                s.close()
                return
            except OSError:
                s.close()
                time.sleep(0.05)
        raise AssertionError('uci-server 未能监听端口 %d' % self._port)

    def _stop_server(self):
        if self._server and self._server.poll() is None:
            self._server.terminate()
            try:
                self._server.wait(timeout=5)
            except Exception:
                self._server.kill()

    # ---- 状态操作（唯一数据源在服务侧）----

    def _client(self, argv):
        s = socket.socket()
        s.settimeout(30)
        s.connect(('127.0.0.1', self._port))
        s.sendall((json.dumps(argv) + '\n').encode('utf-8'))
        buf = b''
        while not buf.endswith(b'\n'):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.decode('utf-8').strip())

    def value(self, path):
        r = self._client(['get', path])
        return r['out'] if r['rc'] == 0 else None

    def exists(self, path):
        return self.value(path) is not None

    def set(self, section, key, value, pkg='ismart'):
        """改配置并 commit，让后续脚本调用能看到。"""
        r = self._client(['set', '%s.%s.%s=%s' % (pkg, section, key, value)])
        if r['rc'] != 0:
            raise AssertionError('set %s.%s.%s 失败' % (pkg, section, key))
        self._client(['commit', pkg])

    def read_conf(self, pkg):
        with open(os.path.join(self.conf_dir, pkg), encoding='utf-8') as f:
            return f.read()

    # ---- 执行 shell ----

    def sh(self, script, allow_fail=False):
        """在 shell 里跑一段脚本，注入 mock uci 与临时根目录。"""
        env = dict(os.environ)
        env['ISMART_ROOT'] = self.root
        env['ISMART_NO_RELOAD'] = '1'
        env['ISMART_TEST_PORT'] = str(self._port)
        # shim 已在 __init__ 里生成，这里直接复用
        env['PATH'] = os.path.join(self.root, 'bin') + os.pathsep + env.get('PATH', '')

        # commit_all 让脚本里的改动落盘，便于 Python 侧读文件断言。
        # 调用处必须写裸命令名 commit_all 而不是 commit_all()：
        # 部分 shell（Git-Bash 里的 dash）会把 "cmd()" 这种带空括号的
        # 形式误判成函数定义并报 "syntax error: unexpected end of file"。
        # 定义处则必须带括号，两边写法不同是有意为之。
        prelude = (
            'commit_all() {\n'
            '\tuci commit network\n'
            '\tuci commit dhcp\n'
            '\tuci commit firewall\n'
            '\tuci commit ismart\n'
            '}\n'
        )

        # 落盘再执行，而不是 sh -c '<多行脚本>'。
        # Windows 上把多行文本直接塞进 argv 传给 sh 会出现难解释的
        # "syntax error: unexpected end of file"——行号甚至指向不存在的行。
        # 写成文件后由 sh 自己读，行为与 CI 的 Linux 完全一致。
        script_path = os.path.join(self.root, 'run-%d.sh' % self._seq)
        self._seq += 1
        with open(script_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(prelude + script)

        try:
            r = subprocess.run(['sh', script_path], env=env,
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise AssertionError('脚本执行超时（疑似死循环）:\n%s' % script)

        if r.returncode != 0 and not allow_fail:
            raise AssertionError(
                '脚本失败 rc=%s\n--- 脚本 ---\n%s\n--- stdout ---\n%s\n--- stderr ---\n%s'
                % (r.returncode, open(script_path, encoding='utf-8').read(),
                   r.stdout, r.stderr))
        return r.stdout, r.stderr

    def source(self, body, allow_fail=False):
        """source core.sh 后执行一段 body。"""
        return self.sh('. "%s"\n%s' % (CORE.replace('\\', '/'), body), allow_fail)

    def out(self, body):
        return self.source(body)[0]

    def cleanup(self):
        self._stop_server()
        shutil.rmtree(self.root, ignore_errors=True)


# ------------------------------------------------------------------- 用例

RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


@case
def test_backup_creates_snapshot(e):
    """备份应把三份配置文件原样存下来，并记下当时的 lan IP"""
    e.source('ismart_backup >/dev/null')
    d = os.path.join(e.root, 'etc', 'ismart', 'backup')
    for f in ('network', 'dhcp', 'firewall'):
        p = os.path.join(d, f)
        assert os.path.exists(p), '缺少备份 %s' % f
        assert 'config' in open(p, encoding='utf-8').read(), '%s 备份内容异常' % f
    got = open(os.path.join(d, 'lan_ip'), encoding='utf-8').read().strip()
    assert got == '192.168.1.1', '备份未记录 lan IP: %s' % got


@case
def test_preflight_rejects_same_ip_as_gateway(e):
    """本机 IP 与网关相同必须拦下，否则会自己给自己当网关"""
    e.set('main', 'lan_ipaddr', '192.168.1.1')
    out, err = e.source('ismart_preflight; echo rc=$?', allow_fail=True)
    assert 'rc=0' not in out, 'IP 与网关相同时未拦截: %s' % out
    assert '自己给自己当网关' in err, '错误信息未指明原因: %s' % err


@case
def test_preflight_rejects_bad_netmask(e):
    """非法掩码必须在切换前拦下"""
    e.set('main', 'lan_netmask', '255.255.0')
    out, err = e.source('ismart_preflight; echo rc=$?', allow_fail=True)
    assert 'rc=0' not in out, '非法掩码未拦截: %s' % out
    assert '子网掩码不合法' in err, '错误信息未指明原因: %s' % err


@case
def test_preflight_rejects_missing_lan_section(e):
    """网口段名写错时必须报错，而不是把网络改坏"""
    e.set('main', 'lan_section', 'nosuchif')
    out, err = e.source('ismart_preflight; echo rc=$?', allow_fail=True)
    assert 'rc=0' not in out, '段名不存在时应拦截: %s' % out
    assert 'lan 接口名' in err, '错误信息未指明原因: %s' % err


@case
def test_netmask_bits(e):
    """掩码换算表要正确，prefix 由它拼出来"""
    for nm, bits in [('255.255.255.0', '24'), ('255.255.0.0', '16'),
                     ('255.255.255.252', '30'), ('255.0.0.0', '8'),
                     ('255.255.255.255', '32')]:
        got = e.out('ismart_netmask_bits %s' % nm).strip()
        assert got == bits, '%s 应为 %s，实际 %s' % (nm, bits, got)
    got = e.out('ismart_netmask_bits 255.255.0 || echo invalid').strip()
    assert got.endswith('invalid'), '非法掩码未返回失败，实际 %s' % got


@case
def test_to_bypass_sets_static_lan(e):
    """切旁路由：lan 应变 static、拿到目标 IP、网关指向主路由"""
    e.source('ismart_apply_bypass off; commit_all')

    assert e.value('network.lan.proto') == 'static', 'lan.proto 未改 static'
    assert e.value('network.lan.ipaddr') == '192.168.1.2', 'lan.ipaddr 不对'
    assert e.value('network.lan.netmask') == '255.255.255.0', 'lan.netmask 不对'
    assert e.value('network.lan.gateway') == '192.168.1.1', 'lan.gateway 不对'
    assert e.value('network.wan.proto') == 'none', 'wan 未停用'
    # ip6assign 必须删掉，否则主路由和旁路由会发两套 IPv6
    assert not e.exists('network.lan.ip6assign'), 'ip6assign 未清除'
    mode = open(os.path.join(e.root, 'etc', 'ismart', 'mode')).read().split('\n')[0]
    assert mode == 'bypass', 'mode 文件未记录 bypass'


@case
def test_to_bypass_dhcp_off(e):
    """off 模式：本机不发地址，由主路由负责"""
    e.source('ismart_apply_bypass off; commit_all')
    assert e.value('dhcp.lan.ignore') == '1', 'off 模式应 ignore=1'


@case
def test_to_bypass_dhcp_on(e):
    """on 模式：本机发地址，网关指本机、DNS 指主路由"""
    e.source('ismart_apply_bypass on; commit_all')
    assert not e.exists('dhcp.lan.ignore'), 'on 模式不应 ignore'
    opts = e.value('dhcp.lan.dhcp_option') or ''
    assert '3,192.168.1.2' in opts, 'option 3 未指向本机: %s' % opts
    assert '6,192.168.1.1' in opts, 'on 模式 DNS 应指向主路由: %s' % opts


@case
def test_to_bypass_dhcp_local(e):
    """local 模式：DNS 指向本机，供透明代理 / DNS 劫持用"""
    e.source('ismart_apply_bypass local; commit_all')
    opts = e.value('dhcp.lan.dhcp_option') or ''
    assert '6,192.168.1.2' in opts, 'local 模式 DNS 应指向本机: %s' % opts


@case
def test_to_bypass_firewall(e):
    """防火墙：默认关 NAT，放通 lan→lan 转发，wan 的 NAT 不受影响"""
    e.source('ismart_apply_bypass off; commit_all')

    # lan 是第一个匿名 zone，wan 是第二个
    assert e.value('firewall.@zone[0].masquerade') == '0', '旁路由默认应关 NAT'
    assert e.value('firewall.@zone[1].masquerade') == '1', 'wan 的 NAT 被误改'
    assert e.value('firewall.ismart_lan_lan.src') == 'lan', 'lan→lan src 不对'
    assert e.value('firewall.ismart_lan_lan.dest') == 'lan', 'lan→lan dest 不对'


@case
def test_masq_option_honoured(e):
    """masq=1 时旁路由自己做 NAT"""
    e.set('main', 'masq', '1')
    e.source('ismart_apply_bypass off; commit_all')
    assert e.value('firewall.@zone[0].masquerade') == '1', 'masq=1 未生效'


@case
def test_wan_mode_keep(e):
    """wan_mode=keep 时不动 wan 段"""
    e.set('main', 'wan_mode', 'keep')
    e.source('ismart_apply_bypass off; commit_all')
    assert e.value('network.wan.proto') == 'dhcp', 'wan_mode=keep 不应改 wan'


@case
def test_dns_primary_override(e):
    """单独指定 dns_primary 时 DNS 不跟随网关"""
    e.set('main', 'dns_primary', '223.5.5.5')
    e.source('ismart_apply_bypass on; commit_all')
    opts = e.value('dhcp.lan.dhcp_option') or ''
    assert '6,223.5.5.5' in opts, 'dns_primary 未生效: %s' % opts


@case
def test_repeated_switch_no_duplicate_forwarding(e):
    """反复切换不应堆出多条 lan→lan 转发

    用具名段 ismart_lan_lan 的存在性来判定「转发还在」，
    并单独数 dest=lan 的 forwarding 段数量来判定「没有变多」。
    只看 dest 是不够的：name='lan'、network='lan' 都会以 'lan' 结尾，
    早期版本用 endswith("='lan'") 数，结果把 zone 的属性也算进去了。
    """
    for _ in range(3):
        e.source('ismart_apply_bypass off; commit_all')

    assert e.value('firewall.ismart_lan_lan.dest') == 'lan', 'lan→lan 转发丢失'

    shown = e.out('uci show firewall')
    dest_lan = [l for l in shown.split('\n')
                if '.dest=' in l and l.endswith("='lan'")]
    assert len(dest_lan) == 1, \
        'lan→lan 转发应恰好 1 条，实际 %d 条: %s' % (len(dest_lan), dest_lan)


@case
def test_router_restore_roundtrip(e):
    """切走再切回，三份配置应与原始完全一致（逐字节比对）"""
    e.source('ismart_backup >/dev/null; ismart_apply_bypass off >/dev/null; commit_all')
    assert e.value('network.lan.proto') == 'static', '前置条件失败：未切到旁路由'

    bdir = os.path.join(e.root, 'etc', 'ismart', 'backup')
    originals = {f: open(os.path.join(bdir, f), encoding='utf-8').read()
                 for f in ('network', 'dhcp', 'firewall')}

    e.source('ismart_apply_router >/dev/null; commit_all')
    for f, want in originals.items():
        got = e.read_conf(f)
        assert got == want, '%s 恢复后与备份不一致\n--- 期望 ---\n%s\n--- 实际 ---\n%s' % (f, want, got)

    mode = open(os.path.join(e.root, 'etc', 'ismart', 'mode')).read().strip()
    assert mode == 'router', 'mode 文件未记录 router'


@case
def test_router_removes_plugin_artifacts(e):
    """切回路由模式必须清掉本插件留下的东西"""
    e.source('ismart_apply_bypass off; commit_all')
    e.source('ismart_apply_router >/dev/null; commit_all')
    assert not os.path.exists(os.path.join(e.root, 'etc', 'sysctl.d', '99-ismart.conf')), \
        '切回后仍残留 sysctl 配置'
    assert not os.path.exists(os.path.join(e.root, 'var', 'run', 'ismart', 'pending')), \
        '切回后仍残留 pending 标记'
    assert not e.exists('firewall.ismart_lan_lan'), '切回后仍残留 lan→lan 转发'


@case
def test_status_json_shape(e):
    """status 必须吐出可解析的 JSON，且字段齐全"""
    e.source('ismart_apply_bypass off; commit_all')
    st = json.loads(e.out('ismart_status_json').strip())
    for k in ('mode', 'consistent', 'lan_section', 'lan_proto', 'configured_ip',
              'active_ip', 'netmask', 'gateway', 'has_backup', 'backup_time',
              'dhcp_active', 'masquerade', 'lan_lan_forwarding', 'pending'):
        assert k in st, 'status 缺少字段 %s' % k
    assert st['mode'] == 'bypass', 'status.mode 不对'
    assert st['consistent'] is True, '刚切换完应判定为一致'
    assert st['lan_lan_forwarding'] is True, 'lan→lan 应报告为已放通'
    assert st['has_backup'] is True, '切换时应自动备份'


@case
def test_status_detects_drift(e):
    """手工改过网络后，consistent 应报 false 而不是继续撒谎"""
    e.source('ismart_apply_bypass off; commit_all')
    e.source('uci set network.lan.ipaddr=10.0.0.9; uci commit network')
    st = json.loads(e.out('ismart_status_json').strip())
    assert st['consistent'] is False, '配置漂移未被检出'


@case
def test_guard_rolls_back_on_timeout(e):
    """观察超时且拿不到预期地址时，必须自动切回路由模式

    制造「探测不到目标地址」的方式是把 network.lan.ipaddr 改成别的值：
    guard 拿的是 pending 里记下的 expect 去比对 ismart_probe_ip，
    而 probe 在没有 ubus/ip 时会退化到读 network.lan.ipaddr。
    注意只改 ismart.main.lan_ipaddr 没用——那个值只在切换时读一次，
    之后再改不会影响已经生效的地址。
    """
    e.source('ismart_backup >/dev/null; ismart_apply_bypass off >/dev/null; commit_all')
    e.set('main', 'guard_timeout', '0')
    e.set('lan', 'ipaddr', '10.9.9.9', pkg='network')

    got = e.out('ismart_guard_check').strip()
    assert 'rolled-back' in got, '超时未触发回滚，实际输出: %s' % got

    mode = open(os.path.join(e.root, 'etc', 'ismart', 'mode')).read().strip()
    assert mode == 'router', '回滚后 mode 应为 router'
    assert not os.path.exists(os.path.join(e.root, 'var', 'run', 'ismart', 'pending')), \
        '回滚后仍残留 pending'
    assert e.value('network.lan.ipaddr') == '192.168.1.1', '回滚后 lan IP 未恢复'


@case
def test_guard_ok_when_ip_matches(e):
    """拿到预期地址时应解除观察，而不是误回滚"""
    e.source('ismart_backup >/dev/null; ismart_apply_bypass off >/dev/null; commit_all')
    got = e.out('ismart_guard_check').strip()
    # PC 上没有 ubus/ip，探测会退化到配置值，正好等于 expect
    assert 'ok' in got, '地址匹配时应判定 ok，实际: %s' % got
    assert not os.path.exists(os.path.join(e.root, 'var', 'run', 'ismart', 'pending')), \
        '判定 ok 后应清除 pending'


@case
def test_guard_idle_without_pending(e):
    """没有 pending 时守护应直接空转退出，不做任何事"""
    got = e.out('ismart_guard_check').strip()
    assert got == 'idle', '无 pending 时应返回 idle，实际: %s' % got


@case
def test_ctl_to_router_without_backup_fails(e):
    """没有备份时切回必须报错，而不是把网络改坏"""
    out, err = e.source('ismart_apply_router; echo rc=$?', allow_fail=True)
    assert 'rc=0' not in out, '无备份切回应返回非零: %s' % out
    assert '没有可用备份' in err, '错误信息不明确: %s' % err


@case
def test_unknown_dhcp_mode_rejected(e):
    """拼错的 DHCP 模式必须报错"""
    out, err = e.source('ismart_apply_bypass bogus; echo rc=$?', allow_fail=True)
    assert 'rc=0' not in out, '未知 DHCP 模式应报错: %s' % out
    assert '未知的 DHCP 模式' in err, '错误信息不明确: %s' % err


@case
def test_zone_index_lookup(e):
    """zone 查找要能按名字定位到匿名段下标"""
    assert e.out('ismart_zone_index lan').strip() == '0', 'lan zone 下标应为 0'
    assert e.out('ismart_zone_index wan').strip() == '1', 'wan zone 下标应为 1'
    got = e.out('ismart_zone_index nosuch || echo none').strip()
    assert got.endswith('none'), '不存在的 zone 应返回失败，实际 %s' % got


@case
def test_custom_lan_section_name(e):
    """非标准网口段名（如 eth_lan）也要能用"""
    files = dict(FILES)
    files['network'] = NETWORK_ROUTER.replace("config interface 'lan'",
                                              "config interface 'eth_lan'")
    files['dhcp'] = DHCP_ROUTER.replace("config dhcp 'lan'", "config dhcp 'eth_lan'")
    files['ismart'] = ISMART_CONF.replace("option lan_section 'lan'",
                                          "option lan_section 'eth_lan'")
    e2 = Env(files)
    try:
        e2.source('ismart_apply_bypass off; commit_all')
        assert e2.value('network.eth_lan.proto') == 'static', '自定义段名未生效'
        assert e2.value('network.eth_lan.ipaddr') == '192.168.1.2', '自定义段名 IP 不对'
        assert e2.value('dhcp.eth_lan.ignore') == '1', '自定义段名 DHCP 未关闭'
    finally:
        e2.cleanup()


# --------------------------------------------------------------------- main

def main():
    if not os.path.exists(CORE):
        print('找不到 core.sh: %s' % CORE)
        return 1

    passed, failed = 0, []
    for fn in RESULTS:
        env = Env()
        name = fn.__name__
        t0 = time.time()
        try:
            fn(env)
            dt = time.time() - t0
            flag = ' <-- 偏慢' if dt > 10 else ''
            print('  ok   %-48s %5.1fs%s' % (name, dt, flag))
            passed += 1
        except Exception as exc:
            dt = time.time() - t0
            print('  FAIL %-48s %5.1fs' % (name, dt))
            print('       %s' % str(exc)[:2500])
            failed.append(name)
        finally:
            env.cleanup()

    print()
    print('通过 %d / %d' % (passed, len(RESULTS)))
    if failed:
        print('失败: %s' % ', '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    # 常驻服务进程的入口
    if '--uci-server' in sys.argv:
        serve(int(os.environ['ISMART_TEST_PORT']),
              sys.argv[sys.argv.index('--uci-server') + 1])
        sys.exit(0)

    # shim 的入口：把一条 uci 命令转发给常驻服务
    if '--uci-client' in sys.argv:
        argv = sys.argv[sys.argv.index('--uci-client') + 1:]
        try:
            s = socket.socket()
            s.settimeout(30)
            s.connect(('127.0.0.1', int(os.environ['ISMART_TEST_PORT'])))
            s.sendall((json.dumps(argv) + '\n').encode('utf-8'))
            buf = b''
            while not buf.endswith(b'\n'):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            resp = json.loads(buf.decode('utf-8').strip())
            if resp['out']:
                sys.stdout.write(resp['out'] + '\n')
            sys.exit(resp['rc'])
        except Exception:
            sys.exit(1)

    sys.exit(main())
