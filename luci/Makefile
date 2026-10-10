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
  # 依赖 ismart-core 而不是自己装 /etc/config/ismart。
  #
  # 真机装出来才发现的问题：两个包都装同一个 conffile 时，
  # apk 会报 "trying to overwrite etc/config/ismart owned by
  # ismart-core" 并计入 error 计数（opkg 只是容忍，不是合法）。
  # 配置文件的所有权应当唯一，归主包。
  DEPENDS:=+luci-base +ismart-core
  PKGARCH:=all
endef

define Package/luci-app-ismart/description
  在「路由模式」与「旁路由模式」之间一键切换的图形界面。
  切换基于 network/dhcp/firewall 三份配置的整体备份与恢复，
  不改写用户的网络结构，并内置超时自动回滚。
endef

# 不声明 conffiles，也不在下面安装 /etc/config/ismart。
# 该文件由 ismart-core 独家拥有（见上面的 DEPENDS 说明），
# 界面包通过依赖拿到它。

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
	# 不安装 ismart.css：样式以 CSS_TEXT 常量内联在 ismart.js 里。
	# 原因见 ismart.js 顶部注释——当前 LuCI 的 'require css/...'
	# 会去找 .../ismart/ismart/css.js 并 404，导致视图白屏。
	# 仓库里保留 .css 源文件仅为便于单独维护，由 CI 保证两边一致。

	# ---- rpcd ACL ----
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_DATA) ./root/usr/share/rpcd/acl.d/luci-app-ismart.json \
		$(1)/usr/share/rpcd/acl.d/luci-app-ismart.json

	# ---- LuCI 菜单 ----
	$(INSTALL_DIR) $(1)/usr/share/luci/menu.d
	$(INSTALL_DATA) ./root/usr/share/luci/menu.d/luci-app-ismart.json \
		$(1)/usr/share/luci/menu.d/luci-app-ismart.json

	# /etc/config/ismart 由 ismart-core 安装，这里不碰：
	# 同一个文件被两个包声明所有权，apk 会判为 error。
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
