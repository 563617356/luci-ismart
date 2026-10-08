#!/bin/sh
# 单独的重载入口：切换后由 ismart-ctl 放到后台调用，
# 保证 HTTP 响应先返回，页面不会因为网络断开而一直转圈。
. /usr/libexec/ismart/core.sh
ismart_reload
