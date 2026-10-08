#!/bin/sh
# 防回滚观察循环。由 procd 以常驻实例方式拉起。
#
# 退出条件只有三种：
#   ok           —— 旁路由 IP 已生效
#   rolled-back  —— 观察超时，已自动切回路由模式
#   pending 文件消失 —— 切换流程被取消
#
# 每次循环都重新判断，不依赖上一次的返回值：
# ismart-ctl guard 内部会自己清 pending，连续调用是安全的。

. /usr/libexec/ismart/core.sh

TAG="ismart-guard"

while [ -f "$(ismart_pending_file)" ]; do
	case "$(ismart_guard_check 2>/dev/null)" in
		ok)
			logger -t "$TAG" -p daemon.info "旁路由地址已生效，解除观察"
			exit 0
			;;
		rolled-back)
			logger -t "$TAG" -p daemon.err "旁路由未生效，已自动切回路由模式"
			exit 0
			;;
	esac
	sleep 5
done

exit 0
