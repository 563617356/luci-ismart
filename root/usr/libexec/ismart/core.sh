#!/bin/sh
# luci-ismart —— 旁路由 / 路由模式互转核心库
#
# 设计原则：**备份-恢复** 而非 *重建*。
#
# 为什么不用「重新生成一份旁路由配置」的做法：
#   OpenWrt 的 /etc/config/network 往往不只有 lan/wan 两段，还混着
#   DSA device 段、802.1Q vlan、无线回程、中继、IPv6 PD 等结构。
#   任何「按模板重写」的做法都会静默丢掉这些结构，用户的网络就废了。
#   因此切走时把 network/dhcp/firewall 三份文件整体备份，切回时整体写回，
#   保证「原样回来」。
#
# 可测试性：
#   所有涉及文件系统的路径都经过 ismart_root 前缀，测试时指向临时目录；
#   uci 命令经过 $ISMART_UCI 覆盖；服务重载可被 ISMART_NO_RELOAD 关闭。
#   于是本文件可以脱离真实 OpenWrt 在 PC 上跑完整逻辑。

ISMART_UCI="${ISMART_UCI:-uci}"
ISMART_ROOT="${ISMART_ROOT:-}"

STATE_SUBDIR="ismart"
RUN_SUBDIR="ismart"

# ---------------------------------------------------------------- 基础工具

ismart_log() {
	# 日志统一走 stderr，stdout 只放命令的结构化输出
	printf '%s\n' "$*" >&2
}

ismart_die() {
	ismart_log "ismart: $*"
	exit 1
}

ismart_have() {
	command -v "$1" >/dev/null 2>&1
}

# uci -q get ismart.main.<opt>，空值回落默认值
ismart_cfg() {
	__v="$($ISMART_UCI -q get "ismart.main.$1" 2>/dev/null)"
	[ -n "$__v" ] || __v="$2"
	printf '%s' "$__v"
}

ismart_cfg_bool() {
	case "$(ismart_cfg "$1" "$2")" in
		1|on|true|yes|enabled) return 0 ;;
		*) return 1 ;;
	esac
}

ismart_esc() {
	# JSON 字符串转义
	printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

ismart_root() {
	printf '%s/%s' "$ISMART_ROOT" "$1"
}

ismart_conf() {
	printf '%s/etc/config/%s' "$ISMART_ROOT" "$1"
}

ismart_state_dir() {
	printf '%s/etc/%s' "$ISMART_ROOT" "$STATE_SUBDIR"
}

ismart_backup_dir() {
	printf '%s/etc/%s/backup' "$ISMART_ROOT" "$STATE_SUBDIR"
}

ismart_run_dir() {
	printf '%s/var/run/%s' "$ISMART_ROOT" "$RUN_SUBDIR"
}

ismart_mode_file() {
	printf '%s/etc/%s/mode' "$ISMART_ROOT" "$STATE_SUBDIR"
}

ismart_pending_file() {
	printf '%s/var/run/%s/pending' "$ISMART_ROOT" "$RUN_SUBDIR"
}

# 相对时间戳，用于防回滚守护
ismart_now() {
	date +%s 2>/dev/null || echo 0
}

# ------------------------------------------------------- UCI / 网络结构查询

# 打印名为 <name> 的防火墙 zone 的匿名 section 索引
# 找不到返回 1。匿名 section 索引在修改配置文件后会变，故每次都重新查找。
ismart_zone_index() {
	__i=0
	while [ "$__i" -lt 32 ]; do
		__n="$($ISMART_UCI -q get "firewall.@zone[$__i].name" 2>/dev/null)"
		[ "$__n" = "$1" ] && { printf '%s' "$__i"; return 0; }
		__i=$((__i + 1))
	done
	return 1
}

ismart_lan_section() {
	ismart_cfg lan_section lan
}

ismart_lan_ip() {
	$ISMART_UCI -q get "network.$(ismart_lan_section).ipaddr" 2>/dev/null
}

ismart_lan_proto() {
	$ISMART_UCI -q get "network.$(ismart_lan_section).proto" 2>/dev/null
}

ismart_lan_gateway() {
	$ISMART_UCI -q get "network.$(ismart_lan_section).gateway" 2>/dev/null
}

# lan zone 是否开了 NAT
ismart_lan_masq() {
	__z="$(ismart_zone_index lan)" || { printf 'unknown'; return; }
	$ISMART_UCI -q get "firewall.@zone[$__z].masquerade" 2>/dev/null || printf '0'
}

# lan zone 是否已配置 lan->lan 同接口转发（本插件加的具名段）
ismart_lan_lan_fwd() {
	$ISMART_UCI -q get "firewall.ismart_lan_lan.dest" 2>/dev/null
}

# dnsmasq 是否在给 lan 发地址
ismart_dhcp_active() {
	case "$($ISMART_UCI -q get "dhcp.$(ismart_lan_section).ignore" 2>/dev/null)" in
		1|on|true|yes) printf '0' ;;
		*) printf '1' ;;
	esac
}

ismart_lan_ip6assign() {
	$ISMART_UCI -q get "network.$(ismart_lan_section).ip6assign" 2>/dev/null
}

# 实际生效的 lan IPv4（ubus 优先，退化到 ip，退化到配置值）
ismart_probe_ip() {
	__sec="$(ismart_lan_section)"
	__dev="$($ISMART_UCI -q get "network.$__sec.device" 2>/dev/null)"
	[ -n "$__dev" ] || __dev="$(ismart_lan_section)"

	# 1) ubus —— 真实设备上一定可用，是 netifd 的权威视图
	if ismart_have ubus; then
		__out="$(ubus call "network.interface.$__sec" status 2>/dev/null)"
		if [ -n "$__out" ]; then
			__ip="$(printf '%s' "$__out" | awk '
				/"ipv4-address"/ { f = 1 }
				f && /"address"/ {
					gsub(/.*"address"[ \t]*:[ \t]*"/, "")
					gsub(/".*/, "")
					print
					exit
				}
			')"
			[ -n "$__ip" ] && { printf '%s' "$__ip"; return 0; }
		fi
	fi

	# 2) ip 命令 —— ubus 不可用时的次选
	if ismart_have ip; then
		__ip="$(ip -4 -o addr show dev "$__dev" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
		[ -n "$__ip" ] && { printf '%s' "$__ip"; return 0; }
	fi

	# 3) 配置值 —— 测试环境 / 极端降级
	ismart_lan_ip
}

# ------------------------------------------------------------------ 备份

ismart_has_backup() {
	[ -f "$(ismart_backup_dir)/network" ]
}

ismart_backup() {
	__dir="$(ismart_backup_dir)"
	mkdir -p "$__dir" || ismart_die "无法创建备份目录 $__dir"

	__ok=0
	for __f in network dhcp firewall; do
		__src="$(ismart_conf "$__f")"
		[ -f "$__src" ] || continue
		# 先写临时文件再改名，避免中途失败留下半份配置
		cp -f "$__src" "$__dir/.$__f.tmp" || ismart_die "备份 $__f 失败"
		mv -f "$__dir/.$__f.tmp" "$__dir/$__f" || ismart_die "备份 $__f 失败"
		__ok=1
	done
	[ "$__ok" = "1" ] || ismart_die "没有任何配置文件可备份"

	date '+%Y-%m-%d %H:%M:%S' > "$__dir/time" 2>/dev/null
	# 记录备份时 lan 的地址，切回后用于一致性比对
	ismart_lan_ip > "$__dir/lan_ip" 2>/dev/null
	ismart_lan_proto > "$__dir/lan_proto" 2>/dev/null
	ismart_log "已备份 network/dhcp/firewall → $__dir"
}

ismart_backup_time() {
	[ -f "$(ismart_backup_dir)/time" ] && cat "$(ismart_backup_dir)/time" || printf '未知'
}

# 从备份恢复路由模式（整份文件写回，保证原样）
ismart_restore_configs() {
	ismart_has_backup || ismart_die "没有可用备份，无法恢复路由模式"
	__dir="$(ismart_backup_dir)"
	for __f in network dhcp firewall; do
		[ -f "$__dir/$__f" ] || continue
		cp -f "$__dir/$__f" "$(ismart_conf "$__f")" || ismart_die "恢复 $__f 失败"
		$ISMART_UCI commit "$__f" 2>/dev/null
	done
	ismart_log "已从备份恢复 network/dhcp/firewall"
}

# 清掉本插件在 sysctl 下留下的文件
ismart_remove_sysctl() {
	rm -f "$(ismart_root /etc/sysctl.d/99-ismart.conf)" 2>/dev/null
	rm -f "$(ismart_root /etc/sysctl.conf.d/99-ismart.conf)" 2>/dev/null
}

# ------------------------------------------------------------- 旁路由改造

# 前提检查：网络结构不符合预期时提前退出，避免把设备改到失联
ismart_preflight() {
	__sec="$(ismart_lan_section)"
	$ISMART_UCI -q get "network.$__sec" >/dev/null 2>&1 \
		|| ismart_die "network 中不存在接口段 '$__sec'，请在参数里修正 lan 接口名"

	__ip="$(ismart_cfg lan_ipaddr '')"
	[ -n "$__ip" ] || ismart_die "未配置本机旁路由 IP（ismart.main.lan_ipaddr）"
	__gw="$(ismart_cfg gateway '')"
	[ -n "$__gw" ] || ismart_die "未配置主路由网关（ismart.main.gateway）"
	[ "$__ip" != "$__gw" ] || ismart_die "本机 IP 与网关相同（$__ip），旁路由会自己给自己当网关"

	# 掩码合法性：必须是 1..32
	__nm="$(ismart_cfg lan_netmask 255.255.255.0)"
	__bits="$(ismart_netmask_bits "$__nm")"
	[ -n "$__bits" ] || ismart_die "子网掩码不合法：$__nm"

	return 0
}

ismart_netmask_bits() {
	case "$1" in
		255.255.255.255) printf '32' ;;
		255.255.255.254) printf '31' ;;
		255.255.255.252) printf '30' ;;
		255.255.255.248) printf '29' ;;
		255.255.255.240) printf '28' ;;
		255.255.255.224) printf '27' ;;
		255.255.255.192) printf '26' ;;
		255.255.255.128) printf '25' ;;
		255.255.255.0)   printf '24' ;;
		255.255.254.0)   printf '23' ;;
		255.255.252.0)   printf '22' ;;
		255.255.248.0)   printf '21' ;;
		255.255.240.0)   printf '20' ;;
		255.255.224.0)   printf '19' ;;
		255.255.192.0)   printf '18' ;;
		255.255.128.0)   printf '17' ;;
		255.255.0.0)     printf '16' ;;
		255.254.0.0)     printf '15' ;;
		255.252.0.0)     printf '14' ;;
		255.248.0.0)     printf '13' ;;
		255.240.0.0)     printf '12' ;;
		255.224.0.0)     printf '11' ;;
		255.192.0.0)     printf '10' ;;
		255.128.0.0)     printf '9'  ;;
		255.0.0.0)       printf '8'  ;;
		*) return 1 ;;
	esac
}

# 目标 IP 是否已被占用。只警告不阻止 —— 有时是有意做双 IP。
ismart_conflict_check() {
	__ip="$1"
	[ -n "$__ip" ] || return 0
	ismart_have ping || return 0
	if ping -c 1 -W 1 "$__ip" >/dev/null 2>&1; then
		ismart_log "警告：$__ip 已有响应，可能与其它设备冲突"
		return 0
	fi
	return 0
}

# 切换到旁路由。
#   $1 = off  → 不接管 DHCP（主路由负责发地址，网关指向本机）
#       on   → 本机接管 DHCP，DNS 指向主路由
#       local→ 本机接管 DHCP，DNS 指向本机（透明代理 / DNS 劫持）
ismart_apply_bypass() {
	__mode="${1:-off}"
	ismart_preflight

	__sec="$(ismart_lan_section)"
	__wan="$(ismart_cfg wan_section wan)"
	__ip="$(ismart_cfg lan_ipaddr)"
	__nm="$(ismart_cfg lan_netmask 255.255.255.0)"
	__gw="$(ismart_cfg gateway)"
	__bits="$(ismart_netmask_bits "$__nm")"
	__prefix="${__ip%.*}.0/$__bits"
	__gwip="$(ismart_cfg dns_primary '')"
	[ -n "$__gwip" ] || __gwip="$__gw"

	ismart_conflict_check "$__ip"

	# 未备份时自动先备份一次，切回来才有得恢复
	if ! ismart_has_backup; then
		ismart_log "未发现备份，先做一次自动备份"
		ismart_backup
	fi

	# ---- network：lan 改静态、指向主路由；wan 停用 ----
	$ISMART_UCI set "network.$__sec.proto=static"
	# ipaddr 可能是 list，先整体删掉再写，避免残留多地址
	$ISMART_UCI -q delete "network.$__sec.ipaddr" 2>/dev/null
	$ISMART_UCI add_list "network.$__sec.ipaddr=$__ip"
	$ISMART_UCI -q delete "network.$__sec.netmask" 2>/dev/null
	$ISMART_UCI add_list "network.$__sec.netmask=$__nm"
	$ISMART_UCI set "network.$__sec.gateway=$__gw"
	# 旁路由不做 DHCP 也不分配前缀，删掉原有租约与 PD 残留
	$ISMART_UCI -q delete "network.$__sec.ip6assign" 2>/dev/null
	$ISMART_UCI -q delete "network.$__sec.iptype" 2>/dev/null
	$ISMART_UCI -q delete "network.$__sec.delegate" 2>/dev/null

	case "$(ismart_cfg wan_mode disable)" in
		keep) ismart_log "wan 段保持原样（wan_mode=keep）" ;;
		*)
			if $ISMART_UCI -q get "network.$__wan" >/dev/null 2>&1; then
				$ISMART_UCI set "network.$__wan.proto=none"
				ismart_log "已停用 wan 拨号（proto=none）"
			fi
			;;
	esac
	$ISMART_UCI commit network

	# ---- dhcp ----
	case "$__mode" in
		off)
			$ISMART_UCI set "dhcp.$__sec.ignore=1"
			# odhcpd 的 RA/DHCPv6 一并关掉，否则客户端会收到两套地址
			$ISMART_UCI -q delete "dhcp.$__sec.ra" 2>/dev/null
			$ISMART_UCI -q delete "dhcp.$__sec.dhcpv6" 2>/dev/null
			$ISMART_UCI -q delete "dhcp.$__sec.ndp" 2>/dev/null
			;;
		on|local)
			$ISMART_UCI -q delete "dhcp.$__sec.ignore" 2>/dev/null
			$ISMART_UCI -q delete "dhcp.$__sec.ra" 2>/dev/null
			$ISMART_UCI -q delete "dhcp.$__sec.dhcpv6" 2>/dev/null
			$ISMART_UCI -q delete "dhcp.$__sec.ndp" 2>/dev/null
			$ISMART_UCI -q delete "dhcp.$__sec.dhcp_option" 2>/dev/null
			# option 3 = 网关，option 6 = DNS
			$ISMART_UCI add_list "dhcp.$__sec.dhcp_option=3,$__ip"
			if [ "$__mode" = "local" ]; then
				$ISMART_UCI add_list "dhcp.$__sec.dhcp_option=6,$__ip"
			else
				$ISMART_UCI add_list "dhcp.$__sec.dhcp_option=6,$__gwip"
			fi
			;;
		*)
			ismart_die "未知的 DHCP 模式：$__mode（可选 off / on / local）"
			;;
	esac
	$ISMART_UCI commit dhcp

	# ---- firewall：旁路由默认不做 NAT，并放通同接口转发 ----
	__z="$(ismart_zone_index lan)" || ismart_die "防火墙里找不到名为 lan 的 zone"
	if ismart_cfg_bool masq 0; then
		$ISMART_UCI set "firewall.@zone[$__z].masquerade=1"
	else
		$ISMART_UCI set "firewall.@zone[$__z].masquerade=0"
	fi
	# 旁路由的核心诉求：客户端把本机当网关，流量要从 lan 进来再从 lan 出去。
	# 用具名 section，保证反复切换不会堆出一堆重复的 forwarding。
	$ISMART_UCI -q delete "firewall.ismart_lan_lan" 2>/dev/null
	$ISMART_UCI set "firewall.ismart_lan_lan=forwarding"
	$ISMART_UCI set "firewall.ismart_lan_lan.src=lan"
	$ISMART_UCI set "firewall.ismart_lan_lan.dest=lan"
	$ISMART_UCI commit firewall

	# ---- sysctl：确保转发打开 ----
	if [ -d "$(ismart_root /etc/sysctl.d)" ] || ismart_have sysctl; then
		mkdir -p "$(ismart_root /etc/sysctl.d)" 2>/dev/null
		{
			echo "# 由 luci-ismart 生成，切回路由模式时自动删除"
			echo "net.ipv4.ip_forward=1"
			echo "net.ipv4.conf.all.forwarding=1"
			echo "net.ipv4.conf.all.send_redirects=0"
		} > "$(ismart_root /etc/sysctl.d/99-ismart.conf)" 2>/dev/null
	fi

	# ---- 记录模式与防回滚 ----
	mkdir -p "$(ismart_state_dir)" "$(ismart_run_dir)" 2>/dev/null
	{
		printf 'bypass\n'
		ismart_now
	} > "$(ismart_mode_file)"
	{
		printf 'mode=bypass\n'
		printf 'ts=%s\n' "$(ismart_now)"
		printf 'expect=%s\n' "$__ip"
		printf 'gw=%s\n' "$__gw"
		printf 'prefix=%s\n' "$__prefix"
	} > "$(ismart_pending_file)"

	ismart_reload
	ismart_log "已切换到旁路由模式：lan=$__sec ip=$__ip/$__bits gw=$__gw dhcp=$__mode"
}

# 切回路由模式 = 从备份整体恢复
ismart_apply_router() {
	ismart_restore_configs
	ismart_remove_sysctl

	# 防火墙里的具名段是本插件加的，备份里必然不存在（备份发生在加它之前），
	# 写回文件即等于删除。若备份较旧则显式清理一次。
	$ISMART_UCI -q delete "firewall.ismart_lan_lan" 2>/dev/null
	$ISMART_UCI commit firewall 2>/dev/null

	mkdir -p "$(ismart_state_dir)" "$(ismart_run_dir)" 2>/dev/null
	printf 'router\n' > "$(ismart_mode_file)"
	rm -f "$(ismart_pending_file)"

	ismart_reload
	ismart_log "已切回路由模式"
}

ismart_reload() {
	[ -n "$ISMART_NO_RELOAD" ] && return 0
	[ -d "$(ismart_root /etc/init.d)" ] || return 0

	ismart_have sysctl && [ -f "$(ismart_root /etc/sysctl.d/99-ismart.conf)" ] && \
		sysctl -p "$(ismart_root /etc/sysctl.d/99-ismart.conf)" >/dev/null 2>&1

	for __s in network dnsmasq firewall odhcpd; do
		[ -x "$(ismart_root /etc/init.d/$__s)" ] || continue
		"$(ismart_root /etc/init.d/$__s)" reload >/dev/null 2>&1 || \
			"$(ismart_root /etc/init.d/$__s)" restart >/dev/null 2>&1
	done
	return 0
}

# 只重载不改配置 —— 用户在页面改了参数点「应用」时用
ismart_apply() {
	__m="$(ismart_detect_mode)"
	ismart_reload
	ismart_log "已按当前配置重载（模式：$__m）"
}

# ------------------------------------------------------------------ 状态

# 检测模式：
#   以 mode 文件为准，但交叉校验实际 UCI 状态；
#   不一致时仍返回文件里的值，另用 consistent 字段告知页面。
ismart_detect_mode() {
	__f="$(ismart_mode_file)"
	if [ -f "$__f" ]; then
		head -n1 "$__f" 2>/dev/null
	else
		printf 'unknown'
	fi
}

# 模式与实际配置是否吻合
ismart_consistent() {
	__m="$(ismart_detect_mode)"
	__ip="$(ismart_lan_ip)"
	__gw="$(ismart_lan_gateway)"
	case "$__m" in
		bypass)
			[ "$(ismart_lan_proto)" = "static" ] || return 1
			[ -n "$__gw" ] || return 1
			[ "$__ip" = "$(ismart_cfg lan_ipaddr '')" ] || return 1
			return 0
			;;
		router)
			ismart_has_backup || return 1
			__b="$(cat "$(ismart_backup_dir)/lan_ip" 2>/dev/null)"
			[ -n "$__b" ] && [ "$__ip" != "$__b" ] && return 1
			return 0
			;;
		*)
			return 1
			;;
	esac
}

ismart_status_json() {
	__m="$(ismart_detect_mode)"
	__ip="$(ismart_lan_ip)"
	__probe="$(ismart_probe_ip)"

	if ismart_consistent; then __c=true; else __c=false; fi
	if ismart_has_backup; then __hb=true; else __hb=false; fi

	# 防回滚守护状态
	__pf="$(ismart_pending_file)"
	if [ -f "$__pf" ]; then
		__ts="$(awk -F= '$1=="ts"{print $2}' "$__pf" 2>/dev/null)"
		__now="$(ismart_now)"
		__el=$(( __now - ${__ts:-0} ))
		[ "$__el" -lt 0 ] && __el=0
		printf '{"pending":true,"elapsed":%s,' "$__el"
	else
		printf '{"pending":false,"elapsed":0,'
	fi

	printf '"mode":"%s","consistent":%s,' "$(ismart_esc "$__m")" "$__c"
	printf '"lan_section":"%s",' "$(ismart_esc "$(ismart_lan_section)")"
	printf '"lan_proto":"%s",' "$(ismart_esc "$(ismart_lan_proto)")"
	printf '"configured_ip":"%s",' "$(ismart_esc "$__ip")"
	printf '"active_ip":"%s",' "$(ismart_esc "$__probe")"
	printf '"netmask":"%s",' "$(ismart_esc "$(ismart_cfg lan_netmask 255.255.255.0)")"
	printf '"gateway":"%s",' "$(ismart_esc "$(ismart_lan_gateway)")"
	printf '"has_backup":%s,' "$__hb"
	printf '"backup_time":"%s",' "$(ismart_esc "$(ismart_backup_time)")"
	printf '"dhcp_active":%s,' "$(ismart_dhcp_active)"
	printf '"masquerade":"%s",' "$(ismart_esc "$(ismart_lan_masq)")"
	printf '"lan_lan_forwarding":%s,' "$([ -n "$(ismart_lan_lan_fwd)" ] && echo true || echo false)"
	printf '"ip6assign":"%s",' "$(ismart_esc "$(ismart_lan_ip6assign)")"
	printf '"uptime":%s' "$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)"
	printf '}\n'
}

# ------------------------------------------------------------- 防回滚守护

ismart_guard_clear() {
	rm -f "$(ismart_pending_file)" 2>/dev/null
}

# 切换后调用：确认实际 IP 生效则解除待观察状态，否则按备份回滚
ismart_guard_check() {
	__pf="$(ismart_pending_file)"
	[ -f "$__pf" ] || { printf 'idle'; return 0; }

	__expect="$(awk -F= '$1=="expect"{print $2}' "$__pf" 2>/dev/null)"
	__ts="$(awk -F= '$1=="ts"{print $2}' "$__pf" 2>/dev/null)"
	__timeout="$(ismart_cfg guard_timeout 90)"
	__now="$(ismart_now)"
	__el=$(( __now - ${__ts:-0} ))

	__ip="$(ismart_probe_ip)"
	if [ -n "$__expect" ] && [ "$__ip" = "$__expect" ]; then
		ismart_guard_clear
		printf 'ok'
		return 0
	fi

	if [ "$__el" -ge "$__timeout" ]; then
		ismart_log "观察 ${__el}s 仍未拿到预期地址 $__expect（当前 ${__ip:-无}），自动回滚"
		ismart_guard_clear
		ismart_apply_router
		printf 'rolled-back'
		return 0
	fi

	printf 'waiting'
}
