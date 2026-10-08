#
# ismart-core — 旁路由模式切换（运行时）
#
# 纯 shell 实现，无编译依赖，PKGARCH:=all，一个包通吃所有架构。
#
include $(TOPDIR)/rules.mk

PKG_NAME:=ismart-core
PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_MAINTAINER:=563617356

include $(INCLUDE_DIR)/package.mk

define Package/ismart-core
  SECTION:=utils
  CATEGORY:=Utilities
  SUBMENU:=3. Applications
  TITLE:=旁路由模式切换（运行时）
  # 只依赖 busybox：awk / sed / grep / cut / tr / date / ping 都由它提供。
  #
  # 不要写 +awk —— OpenWrt 没有独立的 awk 包，awk 是 busybox 的 applet。
  # 也不要写 +ip-tiny —— 那不是有效包名（有效的是 ip-full / ip-tiny 只在
  # 某些 feeds 里出现）。写了会得到：
  #   WARNING: Makefile 'package/ismart-core/Makefile' has a dependency
  #            on 'ip-tiny', which does not exist
  # 该警告不阻断构建，但依赖实际上没被声明，等于没写。
  #
  # `ip` 命令本插件只作可选降级路径（ubus 不可用时才用），
  # 缺失时核心功能不受影响，故不强制依赖。
  DEPENDS:=+busybox
  PKGARCH:=all
endef

define Package/ismart-core/description
  旁路由 / 路由模式互转的核心脚本与命令行工具 ismart-ctl。
  切换时整体备份并改写 network/dhcp/firewall，切回时原样恢复；
  切换后若新地址未在超时时间内生效，守护进程会自动回滚。
endef

define Package/ismart-core/conffiles
/etc/config/ismart
endef

define Build/Prepare
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/ismart-core/install
	# ---- 核心库 ----
	# core.sh 只被 source 引用，不需要可执行位；
	# apply-reload.sh 由 ismart-ctl 以路径直接执行，必须给权限。
	$(INSTALL_DIR) $(1)/usr/libexec/ismart
	$(INSTALL_DATA) ./root/usr/libexec/ismart/core.sh \
		$(1)/usr/libexec/ismart/core.sh
	$(INSTALL_BIN) ./root/usr/libexec/ismart/apply-reload.sh \
		$(1)/usr/libexec/ismart/apply-reload.sh
	$(INSTALL_BIN) ./root/usr/libexec/ismart/guard-loop.sh \
		$(1)/usr/libexec/ismart/guard-loop.sh

	# ---- 命令行入口 ----
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) ./root/usr/bin/ismart-ctl $(1)/usr/bin/ismart-ctl

	# ---- 防回滚守护 ----
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./root/etc/init.d/ismart $(1)/etc/init.d/ismart

	# ---- UCI 配置 ----
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./root/etc/config/ismart $(1)/etc/config/ismart
endef

define Package/ismart-core/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	/etc/init.d/rpcd reload 2>/dev/null
	# 守护服务 enable 一下才能开机自启；
	# 部分固件的 init 脚本不支持 enable 时静默跳过，不影响手动切换。
	/etc/init.d/ismart enable 2>/dev/null || true
	exit 0
}
endef

define Package/ismart-core/prerm
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	# 卸载前把网络配置恢复回去，否则用户会失去 LuCI 访问。
	# 没备份就没什么可恢复的，静默退出即可。
	[ -f /etc/ismart/backup/network ] && /usr/bin/ismart-ctl to-router >/dev/null 2>&1
	/etc/init.d/ismart disable 2>/dev/null || true
	exit 0
}
endef

$(eval $(call BuildPackage,ismart-core))
