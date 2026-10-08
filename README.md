# luci-ismart

OpenWrt 旁路由模式一键切换插件。在「路由模式（主路由）」与「旁路由模式」之间切换，带超时自动回滚。

## 设计要点

### 用「整体备份 / 恢复」，不用「按模板重写」

OpenWrt 的 `/etc/config/network` 很少只有 `lan`/`wan` 两段，实际还混着 DSA device 段、802.1Q vlan、无线回程、中继、IPv6 PD 等结构。任何按模板重写的做法都会静默丢掉这些结构。

所以本插件：

- 切到旁路由前，先把 `network` / `dhcp` / `firewall` **三份文件整体备份**到 `/etc/ismart/backup/`
- 切回路由模式时**原样写回**，逐字节保证一致
- 防火墙里用**具名段** `ismart_lan_lan` 承载 lan→lan 转发，反复切换不会堆出重复条目

### 超时自动回滚

切旁路由会改本机 IP。如果改完网口起不来（IP 被占用、掩码写错、网线插错口），设备就失联了。

插件在切换后启动一个守护进程观察一段时间：

- 拿到预期地址 → 解除观察
- 超时（默认 90 秒）仍未生效 → **用备份自动切回路由模式**

所以填错 IP 不会把设备变成砖，耐心等两分钟它自己会回来。

### 切换会让 LuCI 断开

改 IP 必然断连，这是物理限制而非缺陷。插件的做法是：先把 HTTP 响应尽快返回，再把服务重载放到后台，页面立刻显示新地址与跳转链接，而不是一直转圈到超时。

## 安装

从 [Releases](https://github.com/563617356/luci-ismart/releases) 下载对应你 OpenWrt 版本的包：

| 包 | 说明 |
|---|---|
| `luci-app-ismart` | LuCI 界面（服务 → 旁路由模式） |
| `ismart-core` | 运行时脚本与命令行，界面包已自动依赖 |

```sh
# 22.03 / 23.05 / 24.10 用 opkg
opkg install luci-app-ismart*.ipk ismart-core*.ipk
# 25.12 用 apk
apk add luci-app-ismart*.apk ismart-core*.apk
```

包格式随版本变，**别按「24.10+ 都是 apk」记**——实测对照：

| OpenWrt | 包格式 | 安装工具 |
|---|---|---|
| 22.03.7 | ipk | `opkg` |
| 23.05.6 | ipk | `opkg` |
| 24.10.8 | ipk | `opkg` |
| 25.12.5 | apk | `apk` |

注意 24.10 的文件名带 `-r1` 后缀（`..._1.0.0-r1_all_24.10.8.ipk`），
那是版本号格式变化，不是 apk——扩展名才是判据。

两个包都是 `PKGARCH:=all`，**架构无关**——同一份文件在 x86-64 与
aarch64 路由器上都能装，**不需要找「arm 版」**。

### aarch64 路由器

已验证可在下列 aarch64 目标上正常构建：

| 设备架构 | OpenWrt 版本 | 说明 |
|---|---|---|
| 通用 ARMv8 | 23.05 / 24.10 / 25.12 | `armsr-armv8`，覆盖大部分 ARM64 软路由与开发板 |
| 高通 IPQ807x | 24.10 / 25.12 | `qualcommax-ipq807x`，小米 AX6000 等 |
| 瑞芯微 ARMv8 | 22.03 / 23.05 / 24.10 / 25.12 | `rockchip-armv8` |
| 联发科 Filogic | 23.05 / 24.10 / 25.12 | `mediatek-filogic` |

文件名里**没有架构后缀**是故意的：早期版本叫
`..._24.10.8_x86-64.ipk`，容易被误解成「只能装在 x86 上」。
包本身是 `all`，x86 和 arm64 装的是同一个文件。

确认自己的架构：

```sh
opkg print-architecture
# 或
apk --print-arch
uname -m          # aarch64 / armv7l / x86_64
```

> 22.03 没有 `armsr` 目录（通用 ARM64 从 23.05 起才提供），
> 该版本的 aarch64 走 `rockchip-armv8` / `bcm27xx-bcm2711`。
> 22.03 的 `mediatek` 下只有 mt7621/mt7622/mt7623/mt7629，属 ARMv7。

## 使用

界面：**服务 → 旁路由模式**

首次使用建议：

1. 填好「本机 IP」与「主路由网关」，确认两者在同一网段且不冲突
2. 点「立即备份当前配置」——虽然首次切换会自动备份，但显式备份一次更稳妥
3. 点「保存并应用」
4. 点「切换到旁路由」，按提示用新地址重新访问

命令行等价操作：

```sh
ismart-ctl status            # 查看当前状态（JSON）
ismart-ctl to-bypass         # 切到旁路由
ismart-ctl to-router         # 切回路由模式
ismart-ctl toggle            # 互切
ismart-ctl backup            # 手动备份
ismart-ctl probe             # 打印当前实际生效的 lan IP
```

## 参数

`/etc/config/ismart`：

| 选项 | 默认 | 说明 |
|---|---|---|
| `lan_section` | `lan` | `network` 中的接口段名 |
| `wan_section` | `wan` | 要停用拨号的接口段 |
| `lan_ipaddr` | `192.168.1.2` | 旁路由模式下本机 IP |
| `lan_netmask` | `255.255.255.0` | 主路由所在网段掩码 |
| `gateway` | `192.168.1.1` | 主路由 IP，同时是下一跳 |
| `dns_primary` | 空 | 留空则跟随 `gateway` |
| `masq` | `0` | 旁路由是否对下游做 NAT |
| `wan_mode` | `disable` | `disable` 停用 WAN 拨号；`keep` 保持原样 |
| `guard_timeout` | `90` | 切换后等待新地址生效的秒数，超时自动回滚 |

DHCP 有三种模式，在切换时指定：

| 模式 | 行为 |
|---|---|
| `--dhcp-off`（默认） | 本机不发地址，由主路由负责，网关指向本机 |
| `--dhcp` | 本机发地址，网关指本机、DNS 指主路由 |
| `--dhcp-local` | 本机发地址，DNS 指本机（透明代理 / DNS 劫持） |

## 开发

```sh
# 单元测试（mock uci，不依赖真实 OpenWrt）
python3 tests/test_ismart.py

# shell 语法检查
sh -n root/usr/libexec/ismart/core.sh
```

测试用 Python 实现了一个最小 uci 替身，覆盖 `core.sh` 用到的全部命令，因此完整的切换流程可以在 PC 上跑完并断言结果——这类测试的价值在于，写错一个 option 名在设备上表现为「编译安装全成功、功能全无」，而在测试里会直接失败。

## 仓库结构

```
.
├── Makefile                 运行时主包 ismart-core
├── luci/Makefile            界面包 luci-app-ismart
├── htdocs/                  LuCI 前端（view + css）
├── root/
│   ├── etc/config/          UCI 配置
│   ├── etc/init.d/          防回滚守护（procd）
│   ├── usr/bin/ismart-ctl   命令行入口
│   ├── usr/libexec/ismart/  core.sh 等核心逻辑
│   └── usr/share/           菜单与 rpcd ACL
├── tests/test_ismart.py     核心逻辑单元测试
└── .github/workflows/       多版本云编译
```

## 云编译

推送到 `main` 触发构建，打 `v*` tag 额外创建 Release。

覆盖版本：

| OpenWrt | 包格式 |
|---|---|
| 25.12.5 | apk |
| 24.10.8 | apk |
| 23.05.6 | ipk |
| 22.03.7 | ipk |

SDK 文件名在 workflow 里从目录索引动态发现（含 gcc 版本号，且 24.10+ 用 `.tar.zst`、23.05- 用 `.tar.xz`），不硬编码。

## License

MIT
