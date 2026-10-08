#
# luci-app-ismart — 旁路由模式切换（LuCI 界面）
#
# 不使用 luci.mk，改为标准 package.mk + 手动安装。
#
# 原因（与 luci-app-wloc 相同）：
#   luci.mk 依赖 $(TOPDIR)/feeds/luci/luci.mk，且其包收集走 feeds 的
#   目录扫描。在 CI 里以本地目录引入时，走 luci.mk 的界面包可能不被收录：
#     WARNING: No feed for package 'luci-app-ismart' found
#     make: *** No rule to make target 'package/luci-app-ismart/compile'
#   手动安装反而更可控：装什么、装到哪都写死在下面。
#
include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-ismart
PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_MAINTAINER:=563617356

include $(INCLUDE_DIR)/package.mk

define Package/luci-app-ismart
  SECTION:=luci
  CATEGORY:=LuCI
  SUBMENU:=3. Applications
  TITLE:=旁路由模式切换（界面）
  DEPENDS:=+luci-base
  PKGARCH:=all
endef

define Package/luci-app-ismart/description
  在「路由模式」与「旁路由模式」之间一键切换的图形界面。
  切换基于 network/dhcp/firewall 三份配置的整体备份与恢复，
  不改写用户的网络结构，并内置超时自动回滚。
endef

define Package/luci-app-ismart/conffiles
/etc/config/ismart
endef

# 全部是预置文件，无源码需要编译
define Build/Prepare
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/luci-app-ismart/install
	# ---- 前端资源 -> /www ----
	$(INSTALL_DIR) $(1)/www/luci-static/resources/view/ismart
	$(INSTALL_DATA) ./htdocs/luci-static/resources/view/ismart/ismart.js \
		$(1)/www/luci-static/resources/view/ismart/ismart.js
	$(INSTALL_DATA) ./htdocs/luci-static/resources/view/ismart/ismart.css \
		$(1)/www/luci-static/resources/view/ismart/ismart.css

	# ---- rpcd ACL ----
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_DATA) ./root/usr/share/rpcd/acl.d/luci-app-ismart.json \
		$(1)/usr/share/rpcd/acl.d/luci-app-ismart.json

	# ---- LuCI 菜单 ----
	$(INSTALL_DIR) $(1)/usr/share/luci/menu.d
	$(INSTALL_DATA) ./root/usr/share/luci/menu.d/luci-app-ismart.json \
		$(1)/usr/share/luci/menu.d/luci-app-ismart.json

	# ---- UCI 配置（主包也会装同一份，此处保证单独装界面包时可用）----
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./root/etc/config/ismart $(1)/etc/config/ismart
endef

define Package/luci-app-ismart/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	rm -f /tmp/luci-indexcache.* 2>/dev/null
	rm -rf /tmp/luci-modulecache/ 2>/dev/null
	/etc/init.d/rpcd reload 2>/dev/null
	exit 0
}
endef

$(eval $(call BuildPackage,luci-app-ismart))
