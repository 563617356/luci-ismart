/*
 * 视图的 CSS 不会自动加载，需要在 JS 里显式引入。
 * 路径以 /luci-static/ 开头，指向包安装后的 /www/luci-static/。
 */
'use strict';

/*
 * 依赖声明一律用带引号的 'require xxx' 形式。
 *
 * 不要写成 `require css/ismart/ismart.css;` 这种裸调用——那是把
 * 依赖当普通 JS 表达式求值，浏览器解析到 `css/ismart/...` 里的
 * 标识符 css 就抛 "SyntaxError: Unexpected identifier 'css'"，
 * 整个视图白屏。LuCI 的依赖是靠预处理器扫描这些字符串字面量收集的，
 * 裸 require 不会被识别，也就没有模块被注入。
 */
'require view';
'require uci';
'require fs';
'require dom';
'require ui';
'require poll';
'require rpc';

/*
 * 样式内联注入，不用 'require css/...'。
 *
 * 这条路在当前 LuCI（openwrt-25.12 / bootstrap 主题）上走不通：
 * LuCI 会把它当成普通 JS 模块，去找
 *   /luci-static/resources/css/ismart/ismart/css.js
 * 而该文件不存在，于是 404 —— 视图同样白屏。
 * 真机上 grep 全盘 /www/luci-static/resources/ 也确认：
 * 本机所有能正常工作的 LuCI 应用（vohive、djonehub）都没用 require css，
 * 那个 resources/css/ 目录压根不存在。
 *
 * 所以直接把样式文本插进 <head>。代价是这段 CSS 同时留在 .css 文件里
 * （便于单独维护），但安装包不再安装它——两处不一致由下面的
 * CSS_TEXT 常量作为唯一事实来源，CI 有一致性检查盯着。
 */
var CSS_TEXT = [
	'.ismart-table { width: 100%; margin: 0; }',
	'.ismart-table td { padding: 6px 10px; vertical-align: top;',
	'  border-bottom: 1px solid rgba(128,128,128,0.18); }',
	'.ismart-td-key { width: 190px; font-weight: 600; color: #6b7280; }',
	'.ismart-td-val { word-break: break-all; }',
	'.ismart-badge-row { margin: 4px 0 12px 0; }',
	'.ismart-badge { display: inline-block; padding: 5px 16px;',
	'  border-radius: 14px; font-size: 14px; font-weight: 600;',
	'  line-height: 1.4; letter-spacing: 0.5px; }',
	'.ismart-badge-bypass { background: #e0f2fe; color: #075985; }',
	'.ismart-badge-router { background: #dcfce7; color: #166534; }',
	'.ismart-badge-unknown { background: #f3f4f6; color: #4b5563; }',
	'.ismart-btn-row { display: flex; flex-wrap: wrap; gap: 16px;',
	'  margin: 12px 0 4px 0; }',
	'.ismart-btn-cell { flex: 1 1 260px; min-width: 240px; }',
	'.ismart-big-btn { width: 100%; padding: 14px 12px; font-size: 15px;',
	'  font-weight: 600; }',
	'.ismart-big-btn[disabled] { opacity: 0.5; cursor: not-allowed; }',
	'.ismart-addr { margin: 10px 0; font-size: 18px; }',
	'.ismart-addr-link { font-family: monospace; font-weight: 700;',
	'  text-decoration: underline; word-break: break-all; }'
].join('\n');

function injectStyle() {
	var id = 'ismart-view-style';

	if (document.getElementById(id))
		return;

	var el = E('style', { 'id': id });
	el.appendChild(document.createTextNode(CSS_TEXT));
	document.head.appendChild(el);
}

/* ismart-ctl 的路径。ACL 里已放行 exec。
 *
 * 状态也走 fs.exec 而不另开 ubus 方法：
 * 少一条通道就少一处命名约定与版本差异的坑，
 * 而 status 本身只是执行一个本地脚本，rpcd 完全能应付。 */
var CTL = '/usr/bin/ismart-ctl';

/*
 * 切换会改本机 IP，LuCI 的 HTTP 连接必然中断。
 * 所以不能等请求返回——用 fs.exec 发出即返回，
 * 立刻弹「请用新地址访问」的提示，不阻塞 UI。
 */
function runCtl(args) {
	return fs.exec(CTL, args);
}

function parseJson(s) {
	try { return JSON.parse(s); } catch (e) { return null; }
}

return view.extend({
	/*
	 * 必须提供 handleSaveApply，否则 LuCI 认为本页不支持保存，
	 * **连保存按钮都不会渲染**——真机上就是「保存按钮数 = 0」，
	 * 参数只能改、存不进去。
	 *
	 * 这里只提交 ismart 这一份配置并 apply，不碰 network/dhcp/firewall。
	 * 真正的网络切换由「一键切换」按钮走 ismart-ctl 完成，
	 * 与本页保存是两条独立的路径。
	 */
	handleSaveApply: function () {
		this.collectParams();

		return uci.save().then(function () {
			return rpc.call('uci', 'apply', {
				rollback: true,
				timeout: 10
			});
		}).then(function () {
			/*
			 * ui.addNotification 的第一个参数是通知 id，**不能传 null**。
			 * LuCI 内部会对它调 charAt，传 null 就抛
			 * "opt.charAt is not a function" —— 而且这个异常发生在
			 * Promise 链里，表现为「点了保存但配置没写进去」，
			 * 控制台只有一条看不出所以然的报错。
			 * 用一个固定字符串当 id 即可。
			 */
			ui.addNotification('ismart-saved', E('p', [
				_('参数已保存。'),
				' ',
				E('a', {
					'href': L.href('admin/services/ismart'),
					'class': 'cbi-link',
					'onclick': function () { location.reload(); }
				}, _('返回本页查看当前生效状态'))
			]), E('p', { 'class': 'cbi-value-description' }, [
				_('注意：改参数不会自动切换模式，仍需点下面的切换按钮。')
			]));
		});
	},

	handleSave: function () {
		this.collectParams();

		return uci.save();
	},

	handleReset: function () {
		return uci.load('ismart').then(function () {
			location.reload();
		});
	},

	load: function () {
		injectStyle();

		return Promise.all([
			uci.load('ismart'),
			this.fetchStatus()
		]);
	},

	fetchStatus: function () {
		return runCtl(['status']).then(function (res) {
			return parseJson(res.stdout || '') || {};
		}).catch(function () {
			return {};
		});
	},

	/* 切换后的落地页：引导用户去新地址 */
	renderRedirect: function (target) {
		var url = target ? ('http://' + target + '/cgi-bin/luci/admin/services/ismart') : null;

		var linkAttrs = {
			'href': url || '#',
			'class': 'ismart-addr-link'
		};

		/*
		 * external 不能用 `url ? true : null`。
		 * LuCI DOM 构造器对属性值调 charAt，传 null 就抛
		 * "opt.charAt is not a function"。不设时干脆别设这个属性。
		 */
		if (url)
			linkAttrs['external'] = 'true';

		return E('div', { 'class': 'cbi-map-descr' }, [
			E('h2', {}, [ _('切换已提交') ]),
			E('p', {}, [ _('路由器正在切换网络模式，LuCI 页面会断开属正常现象。') ]),
			E('p', {}, [ _('大约 10 秒后用新的管理地址访问：') ]),
			E('p', { 'class': 'ismart-addr' }, [
				E('a', linkAttrs, target || _('未知') )
			]),
			E('p', { 'class': 'cbi-map-descr' }, [
				E('em', {}, [ _('如果新地址打不开，说明配置未生效，系统会在约 90 秒后自动切回原模式。') ])
			])
		]);
	},

	renderStatus: function (st) {
		var mode = st['mode'] || 'unknown';
		var consistent = st['consistent'] === true || st['consistent'] === 'true';
		var pending = st['pending'] === true || st['pending'] === 'true';

		var modeText = {
			bypass:  _('旁路由模式'),
			router:  _('路由模式（主路由）'),
			unknown: _('未初始化')
		}[mode] || mode;

		var badgeCls = {
			bypass:  'ismart-badge-bypass',
			router:  'ismart-badge-router',
			unknown: 'ismart-badge-unknown'
		}[mode] || 'ismart-badge-unknown';

		var rows = [];

		function row(label, value, hint) {
			rows.push(E('tr', {}, [
				E('td', { 'class': 'ismart-td-key' }, [ label ]),
				E('td', { 'class': 'ismart-td-val' }, [
					E('span', {}, [ value ]),
					hint ? E('div', { 'class': 'cbi-value-description' }, [ hint ]) : ''
				])
			]));
		}

		row(_('当前模式'), modeText);
		row(_('配置状态'),
			consistent ? _('与记录一致') : _('与记录不一致，请检查是否被其它插件改动'),
			consistent ? '' : _('模式文件与实际 UCI 配置不匹配，可能是手工改过网络设置'));
		row(_('生效地址'),
			st['active_ip'] || '—',
			(st['configured_ip'] && st['active_ip'] && st['configured_ip'] !== st['active_ip'])
				? _('配置为 ') + st['configured_ip'] + _('，实际生效的是上面的地址')
				: '');
		row(_('网关'), st['gateway'] || _('未设置（路由模式下正常）'));
		row(_('网段掩码'), st['netmask'] || '—');
		row(_('本机 DHCP'),
			(st['dhcp_active'] === true || st['dhcp_active'] === 'true') ? _('发地址') : _('不发地址'));
		row(_('NAT (masquerade)'), st['masquerade'] === '1' ? _('开启') : _('关闭'));
		row(_('lan→lan 转发'),
			(st['lan_lan_forwarding'] === true || st['lan_lan_forwarding'] === 'true') ? _('已放通') : _('未放通'));
		row(_('IPv6 前缀分配'), st['ip6assign'] || _('无'));
		row(_('配置备份'),
			(st['has_backup'] === true || st['has_backup'] === 'true') ? st['backup_time'] : _('尚无备份'),
			(st['has_backup'] === true || st['has_backup'] === 'true')
				? _('切回路由模式时用它恢复')
				: _('首次切换会自动创建'));
		row(_('运行时长'), st['uptime']
			? (Math.floor(st['uptime'] / 3600) + _(' 小时 ') + Math.floor((st['uptime'] % 3600) / 60) + _(' 分'))
			: '—');

		if (pending) {
			row(_('切换观察中'),
				_('正在等待新地址生效，已等待 ') + (st['elapsed'] || 0) + _(' 秒'),
				 _('超时未生效会自动切回原模式'));
		}

		return E('div', { 'class': 'cbi-section' }, [
			E('h3', {}, [ _('运行状态') ]),
			E('div', { 'class': 'cbi-section-node' }, [
				E('p', { 'class': 'ismart-badge-row' }, [
					E('span', { 'class': 'ismart-badge ' + badgeCls }, [ modeText ])
				]),
				E('table', { 'class': 'table ismart-table' }, [ E('tbody', {}, rows) ])
			])
		]);
	},

	/* 大按钮区：一眼看出当前在哪个模式，点一下就换 */
	renderSwitchPanel: function (st) {
		var self = this;
		var hasBackup = st['has_backup'] === true || st['has_backup'] === 'true';

		var toBypass = E('button', {
			'class': 'cbi-button cbi-button-action ismart-big-btn',
			'click': ui.createHandlerFn(self, self.onSwitch, 'bypass')
		}, [ _('切换到旁路由') ]);

		var toRouter = E('button', {
			'class': 'cbi-button cbi-button-reset ismart-big-btn',
			'click': ui.createHandlerFn(self, self.onSwitch, 'router'),
			'disabled': hasBackup ? null : 'disabled'
		}, [ _('切回路由模式') ]);

		return E('div', { 'class': 'cbi-section' }, [
			E('h3', {}, [ _('一键切换') ]),
			E('div', { 'class': 'cbi-section-node' }, [
				E('p', { 'class': 'cbi-value-description' },
					 _('切换会修改本机 IP 并重载网络，LuCI 页面会断开属正常现象，请按提示用新地址重新访问。')),
				E('div', { 'class': 'ismart-btn-row' }, [
					E('div', { 'class': 'ismart-btn-cell' }, [
						toBypass,
						E('div', { 'class': 'cbi-value-description' },
							 _('本机变成纯网关，流量交给主路由 NAT'))
					]),
					E('div', { 'class': 'ismart-btn-cell' }, [
						toRouter,
						E('div', { 'class': 'cbi-value-description' },
							hasBackup ? _('从备份整体恢复原网络配置') : _('需要先有一份备份'))
					])
				])
			])
		]);
	},

	onSwitch: function (ev, target) {
		var self = this;

		if (target === 'router') {
			/* 回滚比前进更需要谨慎：会覆盖当前网络配置 */
			return ui.showModal(_('确认切回路由模式？'), {
				'class': 'cbi-modal',
				body: E('div', { 'class': 'cbi-modal-content' }, [
					E('p', {}, [ _('将用备份恢复 network / dhcp / firewall 三份配置。') ]),
					E('p', {}, [ _('切换后本机 IP 会变回备份中的地址，页面会断开。') ])
				]),
				buttons: [
					E('button', {
						'class': 'cbi-button cbi-button-reset',
						'click': function () { ui.hideModal(); self.doSwitch('router', []); }
					}, [ _('确认切回') ]),
					E('button', {
						'class': 'cbi-button cbi-button-neutral',
						'click': ui.hideModal
					}, [ _('取消') ])
				]
			});
		}

		/* 旁路由：先确认目标地址，避免 IP 填错导致失联 */
		var uciState = uci.get('ismart', 'main') || {};
		var targetIp = uciState.lan_ipaddr || '?';
		var gw = uciState.gateway || '?';

		return ui.showModal(_('确认切换到旁路由？'), {
			'class': 'cbi-modal',
			body: E('div', { 'class': 'cbi-modal-content' }, [
				E('p', {}, [
					E('strong', {}, [ _('本机地址：') ]), targetIp,
					E('br'),
					E('strong', {}, [ _('网关：') ]), gw
				]),
				E('p', {}, [ _('请先确认主路由的地址与上面一致，填错会导致切换后无法访问。') ]),
				E('p', { 'class': 'cbi-value-description' },
					 _('若切换后打不开，系统会在约 90 秒后自动切回原模式。'))
			]),
			buttons: [
				E('button', {
					'class': 'cbi-button cbi-button-action',
					'click': function () { ui.hideModal(); self.doSwitch('bypass', []); }
				}, [ _('确认切换') ]),
				E('button', {
					'class': 'cbi-button cbi-button-neutral',
					'click': ui.hideModal
				}, [ _('取消') ])
			]
		});
	},

	/*
	 * 发出切换请求后立刻切到「已提交」页面。
	 * 请求本身会在网络重载时失败，这是预期行为，不当作错误上报。
	 */
	doSwitch: function (target, extra) {
		var self = this;
		var args = (target === 'bypass') ? ['to-bypass'] : ['to-router'];
		args = args.concat(extra || []);

		runCtl(args).catch(function () { /* 网络已断，忽略 */ });

		var uciState = uci.get('ismart', 'main') || {};
		dom.content(
			document.getElementById('ismart-main'),
			self.renderRedirect(target === 'bypass' ? uciState.lan_ipaddr : null)
		);
		window.scrollTo(0, 0);
	},

	/*
	 * 参数行：一个说明文字 + 一个绑定到 ismart.main 的输入框。
	 *
	 * **不在 change 事件里写 uci 内存态**，而是在 handleSave 时
	 * 由 collectParams() 从 DOM 统一采集。原因：依赖 change 事件
	 * 不可靠——真机上就是「输入框里改了值、点了保存、uci 没变」。
	 * 用户也完全可能用粘贴、退格、方向键改值而不触发 change。
	 *
	 * name 属性就是 uci 的 option 名，采集时按它回填。
	 */
	renderValue: function (label, description, name, def) {
		return E('div', { 'class': 'cbi-value ismart-field' }, [
			E('label', { 'class': 'cbi-value-title' }, [ label ]),
			E('div', { 'class': 'cbi-value-field' }, [
				E('input', {
					'type': 'text',
					'class': 'cbi-input-text',
					'name': name,
					'value': (uci.get('ismart', 'main') || {})[name] || def || '',
					'placeholder': def || ''
				}),
				E('div', { 'class': 'cbi-value-description' }, [ description || '' ])
			])
		]);
	},

	/*
	 * 从 DOM 采集所有具名控件的值，写进 uci 内存态。
	 *
	 * 选择器限定为 .ismart-field [name]，不用宽泛的 [name]——
	 * 页面上还有 LuCI 自己注入的带 name 元素，混进来会出问题。
	 * 每个控件单独 try/catch，一个坏元素不至于让整个保存失败。
	 */
	collectParams: function () {
		document.querySelectorAll('.ismart-field [name]').forEach(function (el) {
			try {
				if (!el.name)
					return;

				/*
				 * 必须是四参数形式 set(conf, section, option, value)。
				 *
				 * 踩过的坑：曾写成 uci.set('ismart', 'main', map) —— 那是
				 * 「批量」写法，但这个版本的 uci.set 签名是四参数，
				 * 第三个参数会被当成 option 名，内部对它调 opt.charAt(0)，
				 * 于是抛 "opt.charAt is not a function"。
				 * 而这个异常在 handleSave 的调用栈里，表现为
				 * 「点了保存、页面没报错、配置一点没变」，极难定位。
				 */
				uci.set('ismart', 'main', el.name, el.value);
			} catch (e) {
				/* 单个控件写不进去就跳过，不影响其它 */
			}
		});
	},

	/* 同 renderValue，但用下拉框。choices 是 [[值, 文本], ...]。 */
	renderListValue: function (label, description, name, choices) {
		var cur = (uci.get('ismart', 'main') || {})[name];

		if (cur === undefined && choices.length)
			cur = choices[0][0];

		return E('div', { 'class': 'cbi-value ismart-field' }, [
			E('label', { 'class': 'cbi-value-title' }, [ label ]),
			E('div', { 'class': 'cbi-value-field' }, [
				E('select', {
					'class': 'cbi-input-select',
					'name': name
				}, choices.map(function (c) {
					/*
					 * selected 属性不能传 null。
					 * LuCI 的 DOM 构造器会对属性值调 charAt 来决定怎么 set，
					 * 传 null 进去就抛 "opt.charAt is not a function"，
					 * 整个 select 渲染失败（真机上每个下拉都报错）。
					 * 不选中时干脆不设这个属性。
					 */
					var attrs = { 'value': c[0] };

					if (c[0] === cur)
						attrs['selected'] = 'selected';

					return E('option', attrs, [ c[1] ]);
				})),
				E('div', { 'class': 'cbi-value-description' }, [ description || '' ])
			])
		]);
	},

	render: function (data) {
		var st = data[1] || {};

		return E('div', { 'id': 'ismart-main' }, [
			E('h2', {}, [ _('旁路由模式切换') ]),
			E('div', { 'class': 'cbi-map-descr' }, [
				E('p', {}, [
					 _('在「路由模式」与「旁路由模式」之间一键切换。'),
					E('br'),
					 _('切换基于配置整体备份/恢复，不会重写你的网络结构，DSA、802.1Q、无线回程等配置都能原样回来。')
				])
			]),
			this.renderStatus(st),
			this.renderSwitchPanel(st),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, [ _('旁路由参数') ]),
				E('div', { 'class': 'cbi-section-node' }, [
					this.renderValue(_('网口段名'), _('network 中的接口段名，多数设备是 lan'), 'lan_section'),
					this.renderValue(_('本机 IP'), _('切换后本机使用的地址，需与主路由同网段且不冲突'), 'lan_ipaddr'),
					this.renderValue(_('子网掩码'), _('主路由所在网段的掩码'), 'lan_netmask'),
					this.renderValue(_('主路由网关'), _('主路由的 IP，同时作为旁路由的下一跳'), 'gateway'),
					this.renderValue(_('DNS 地址'), _('留空则跟随网关地址'), 'dns_primary'),
					this.renderListValue(_('NAT'), _('旁路由是否对下游做 NAT，一般关闭'), 'masq', [
						[ '0', _('关闭（推荐，由主路由 NAT）') ],
						[ '1', _('开启（本机做 NAT）') ]
					]),
					this.renderListValue(_('WAN 处理'), _('旁路由一般不再拨号'), 'wan_mode', [
						[ 'disable', _('停用 WAN 拨号') ],
						[ 'keep', _('保持 WAN 原样') ]
					]),
					this.renderValue(_('回滚等待'), _('切换后等待新地址生效的秒数，超时自动切回'), 'guard_timeout')
				])
			]),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, [ _('备份与维护') ]),
				E('div', { 'class': 'cbi-section-node' }, [
					E('p', { 'class': 'cbi-value-description' },
						 _('备份会保存 network / dhcp / firewall 三份文件的完整副本。首次切到旁路由时若没有备份，会自动创建一份。')),
					E('button', {
						'class': 'cbi-button cbi-button-neutral',
						'click': ui.createHandlerFn(this, this.onBackup)
					}, [ _('立即备份当前配置') ])
				])
			])
		]);
	},

	onBackup: function () {
		var self = this;
		return runCtl(['backup']).then(function (res) {
			var j = parseJson(res.stdout || '');
			ui.addNotification('ismart-backup-done', E('p', [
				_('备份完成：'),
				E('code', {}, [ (j && j.time) || '—' ])
			]), 'info');
			return self.fetchStatus();
		}).then(function (st) {
			ui.addNotification('ismart-status-refreshed',
				E('p', [ _('状态已刷新，切换前请确认参数已保存并应用。') ]), 'warning');
			return st;
		});
	},

	/* 状态轮询：切换过程中页面若还连着，自动刷新到新状态。
	 *
	 * 挂在 ready 而不是 render —— render 会被轮询回调反复调用，
	 * 在里面注册 poll 会导致注册 N 次、回调 N 次，越刷越卡。 */
	ready: function () {
		this.pollStatus();
	},

	pollStatus: function () {
		var self = this;

		poll.add(function () {
			return self.fetchStatus().then(function (st) {
				var main = document.getElementById('ismart-main');
				/* 已经切到引导页就不再抢 DOM */
				if (!main || main.querySelector('.ismart-addr')) return true;

				dom.content(main, self.render(st || {}));
				return true;
			}).catch(function () {
				/* 切换中连接可能短暂不可用，交给下一次轮询 */
				return true;
			});
		}, 5);
	}
});
