/*
 * 视图的 CSS 不会自动加载，需要在 JS 里显式引入。
 * 路径以 /luci-static/ 开头，指向包安装后的 /www/luci-static/。
 */
'use strict';
require css/ismart/ismart.css;

'require view';
'require uci';
'require fs';
'require dom';
'require ui';
'require poll';

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
	handleSaveApply: null,
	handleSave: null,
	handleReset: null,

	load: function () {
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

		return E('div', { 'class': 'cbi-map-descr' }, [
			E('h2', {}, [ _('切换已提交') ]),
			E('p', {}, [ _('路由器正在切换网络模式，LuCI 页面会断开属正常现象。') ]),
			E('p', {}, [ _('大约 10 秒后用新的管理地址访问：') ]),
			E('p', { 'class': 'ismart-addr' }, [
				E('a', {
					'href': url || '#',
					'external': url ? true : null,
					'class': 'ismart-addr-link'
				}, target || _('未知') )
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
			ui.addNotification(null, E('p', [
				_('备份完成：'),
				E('code', {}, [ (j && j.time) || '—' ])
			]), 'info');
			return self.fetchStatus();
		}).then(function (st) {
			ui.addNotification(null, E('p', [ _('状态已刷新，切换前请确认参数已保存并应用。') ]), 'warning');
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
