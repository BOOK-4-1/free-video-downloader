/**
 * 全站共用的「导入 Cookie」流程（首页 + 卡密兑换页）
 *
 *  1. 引导：告诉用户去哪个站登录、从哪复制 Cookie
 *  2. 粘贴：支持 "a=b; c=d"、整行 "Cookie: ..."、Netscape cookie.txt、
 *     DevTools 表格复制（名称 TAB 值，单行也行）、扩展导出的 JSON
 *  3. 校验：调用 /api/cookies/verify
 *       - 给了链接 → 真拿这个链接解析一次，用结果说话
 *       - 没给链接 → 检查是否包含登录态字段（sessionid / SESSDATA / web_session ...）
 *  4. 保存：存 localStorage.vg_cookie，页眉按钮变成「已导入」状态
 *  5. 清除：随时删掉本机保存的内容
 *
 * 依赖：页眉/页脚按钮写 data-action="cookie"；提示框写 #toastRoot
 */
(function () {
  'use strict';

  var KEY = 'vg_cookie';

  var PLATFORM_HINTS = [
    {
      name: '小红书',
      href: 'https://www.xiaohongshu.com',
      key: 'web_session',
      note: '必须登录：登录后在 xiaohongshu.com 打开任意笔记，复制网页请求里的 Cookie'
    },
    {
      name: 'B 站',
      href: 'https://www.bilibili.com',
      key: 'SESSDATA',
      note: '登录后能拿到 1080P / 4K，未登录只有 360P~720P'
    },
    {
      name: '抖音',
      href: 'https://www.douyin.com',
      key: 'sessionid',
      note: '抖音已免 Cookie 支持（1080P 无水印），一般不用导；导入后可支持部分私密 / 需登录内容'
    }
  ];

  /* ---------------- 小工具 ---------------- */

  function toast(text, type) {
    var root = document.getElementById('toastRoot');
    if (!root) return;
    var d = document.createElement('div');
    d.className = 'toast ' + (type || '');
    d.textContent = text;
    root.appendChild(d);
    setTimeout(function () { d.remove(); }, 3200);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /** 提取 Cookie 名，逻辑与后端 downloader.parse_cookie_text 保持一致 */
  function parseCookieNames(text) {
    text = (text || '').trim();
    if (!text) return [];
    var HEADER = ['name', 'value', 'cookie', 'cookie name', 'key', '名称', '值'];
    var valid = function (k) {
      return /^[A-Za-z_][A-Za-z0-9_.\-]*$/.test(k) && HEADER.indexOf(k.toLowerCase()) < 0;
    };
    var names = [];
    function add(k) {
      k = String(k == null ? '' : k).trim();
      if (valid(k)) names.push(k);
    }

    // 扩展导出的 JSON
    if (text.charAt(0) === '[' || text.charAt(0) === '{') {
      try {
        var obj = JSON.parse(text);
        if (!Array.isArray(obj)) obj = Array.isArray(obj.cookies) ? obj.cookies : [obj];
        obj.forEach(function (it) { if (it && typeof it === 'object') add(it.name || it.Name); });
        if (names.length) return names;
      } catch (e) { /* 不是 JSON，继续按文本解析 */ }
    }

    text.split('\n').forEach(function (raw) {
      var line = raw.trim();
      if (!line) return;
      if (line.charAt(0) === '#' && line.toLowerCase().indexOf('#httponly_') !== 0) return;
      if (line.indexOf('\t') >= 0) {
        var cols = line.split('\t').map(function (c) { return c.trim(); });
        // Netscape 7 列，首列是域名；其余按「名称 TAB 值」表格处理
        if (cols.length >= 7 &&
            (cols[0].indexOf('.') >= 0 || cols[0].toLowerCase().indexOf('#httponly_') === 0)) {
          add(cols[5]);
        } else if (cols.length >= 2) {
          add(cols[0]);
        }
        return;
      }
      line.replace(/^cookie:\s*/i, '').split(';').forEach(function (p) {
        if (p.indexOf('=') > 0) { add(p.split('=')[0]); return; }
        var m = /^\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*[:\s]\s*\S/.exec(p);
        if (m) add(m[1]);
      });
    });
    return names;
  }

  /** 判断粘贴内容像哪种格式（与后端 detect_cookie_format 保持一致） */
  function detectCookieFormat(text) {
    var s = (text || '').trim();
    if (!s) return 'empty';
    if (s.charAt(0) === '[' || s.charAt(0) === '{') {
      try { JSON.parse(s); return 'json'; } catch (e) { /* 不是 JSON，继续按文本判断 */ }
    }
    var lines = s.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (!line) continue;
      if (line.charAt(0) === '#' && line.toLowerCase().indexOf('#httponly_') !== 0) continue;
      if (line.indexOf('\t') >= 0) {
        var cols = line.split('\t').map(function (c) { return c.trim(); });
        if (cols.length >= 7 &&
            (cols[0].indexOf('.') >= 0 || cols[0].toLowerCase().indexOf('#httponly_') === 0)) {
          return 'netscape';
        }
        return 'table';
      }
      return 'header';
    }
    return 'unknown';
  }

  var FORMAT_LABEL = {
    header: '请求头 a=b; c=d',
    table: 'DevTools 表格（名称 Tab 值）',
    netscape: 'Netscape cookie.txt',
    json: 'JSON 导出',
    unknown: '未识别',
    empty: '空'
  };
  function formatLabel(k) { return FORMAT_LABEL[k] || '未识别'; }

  /** 本地快速统计：粘贴时就能看到「识别到几项 / 什么格式 / 有没有登录态字段」 */
  function quickSummary(text) {
    var pairs = parseCookieNames(text);
    var LOGIN = ['sessionid', 'sessionid_ss', 'sid_tt', 'SESSDATA', 'web_session', 'sid', 'sso_uid'];
    var hit = pairs.filter(function (n) { return LOGIN.indexOf(n) >= 0; });
    return {
      count: pairs.length,
      hasLogin: hit.length > 0,
      loginKeys: hit,
      format: pairs.length ? detectCookieFormat(text) : 'unknown'
    };
  }

  /**
   * 把用户粘的「值」规范化成标准 Cookie 文本。用户不需要知道格式：
   * 纯值、带引号、已经带名称、DevTools 表格行都能处理。纯函数，暴露给自测。
   */
  function buildCookieFromValue(name, rawValue) {
    var v = String(rawValue == null ? '' : rawValue).trim().split('\n')[0].trim();
    var tab = v.split('\t');
    if (tab.length >= 2 && /^[A-Za-z_][A-Za-z0-9_.\-]*$/.test(tab[0].trim()) && tab[0].indexOf('=') < 0) {
      v = tab[1].trim();
    }
    v = v.replace(/^[A-Za-z_][A-Za-z0-9_.\-]*\s*[=:]\s*/, '').trim();
    v = v.replace(/^["']|["']$/g, '').trim();
    var n = String(name == null ? '' : name).trim() || 'SESSDATA';
    if (!v) return { ok: false, error: '先把复制到的值粘进来' };
    if (!/^[A-Za-z_][A-Za-z0-9_.\-]*$/.test(n)) {
      return { ok: false, error: '名称只能用英文字母、数字、下划线或点' };
    }
    return { ok: true, text: n + '=' + v, name: n, value: v };
  }

  /**
   * 三步诊断：把「格式不对」「缺登录态」「Cookie 失效」明确分开，让用户知道该改什么。
   * verify 为 /api/cookies/verify 返回的 data（可为 null，表示还没验证）。
   * 纯函数，暴露给自测；state: ok | warn | err | idle
   */
  function cookieDiagnosis(text, verify) {
    var raw = (text || '').trim();
    var sum = quickSummary(raw);
    var d = {
      tone: '',
      headline: '还没有粘贴内容 —— 不知道怎么复制的话，点开下面的「格式示例」',
      showExamples: false,
      steps: [
        { key: 'format', label: '① 格式识别', state: 'idle', text: '等待粘贴' },
        { key: 'login', label: '② 登录态字段', state: 'idle', text: '等格式通过后再判断' },
        { key: 'validity', label: '③ 有效性', state: 'idle', text: '填一个视频链接点「验证」才能确认真伪' }
      ]
    };
    if (!raw) return d;

    if (!sum.count) {
      d.tone = 'err';
      d.headline = (verify && verify.stage === 'empty')
        ? '服务端也确认是格式问题：对照示例改格式，不用换 Cookie。'
        : '卡在第 ① 步：格式没识别出来。这和 Cookie 是否过期无关，对照下面的示例改一下格式即可。';
      d.showExamples = true;
      d.steps[0] = { key: 'format', label: '① 格式识别', state: 'err', text: '没识别出任何 Cookie 项 —— 格式问题' };
      d.steps[1] = { key: 'login', label: '② 登录态字段', state: 'idle', text: '格式没通过，暂时无法判断' };
      d.steps[2] = { key: 'validity', label: '③ 有效性', state: 'idle', text: '先把格式改对' };
      return d;
    }

    d.steps[0] = {
      key: 'format', label: '① 格式识别', state: 'ok',
      text: '识别到 ' + sum.count + ' 项（' + formatLabel(sum.format) + '）'
    };
    if (!sum.hasLogin) {
      d.tone = 'warn';
      d.headline = '卡在第 ② 步：格式没问题，但这份 Cookie 没有登录态字段 —— 多半复制了游客 Cookie，或漏了关键一行。不用改格式。';
      d.showExamples = true;
      d.steps[1] = {
        key: 'login', label: '② 登录态字段', state: 'warn',
        text: '缺 SESSDATA / sessionid / web_session 等登录态字段'
      };
      d.steps[2] = { key: 'validity', label: '③ 有效性', state: 'idle', text: '建议重新登录后复制完整 Cookie' };
      d.tone = 'warn';
    } else {
      d.tone = 'ok';
      d.headline = '前两步都通过了，还差第 ③ 步：填一个该平台的视频链接点「验证」，才能确认 Cookie 真的有效。';
      d.steps[1] = { key: 'login', label: '② 登录态字段', state: 'ok', text: '含 ' + sum.loginKeys.join(' / ') };
      d.steps[2] = { key: 'validity', label: '③ 有效性', state: 'idle', text: '尚未验证，点「验证」用真实解析确认' };
    }

    if (verify) {
      if (verify.ok) {
        d.tone = 'ok';
        d.headline = '三步全部通过：这份 Cookie 可以用了，点「保存」。';
        d.steps[2] = { key: 'validity', label: '③ 有效性', state: 'ok', text: verify.message || '校验通过' };
      } else if (verify.stage === 'empty') {
        d.tone = 'err';
        d.headline = '服务端也确认是格式问题：对照示例改格式，不用换 Cookie。';
        d.showExamples = true;
        d.steps[0] = { key: 'format', label: '① 格式识别', state: 'err', text: '服务端没识别出任何 Cookie 项' };
      } else if (verify.stage === 'anonymous') {
        d.tone = 'warn';
        d.headline = '第 ② 步没过：服务端确认格式正确但缺登录态字段，重新登录后复制完整 Cookie。';
        d.steps[1] = { key: 'login', label: '② 登录态字段', state: 'warn', text: '服务端确认：缺登录态字段' };
      } else if (verify.stage === 'auth') {
        d.tone = 'err';
        d.headline = '卡在第 ③ 步：格式和登录态都正常，是 Cookie 本身没被站点接受 —— 多半已过期或失效，重新登录后再复制一次。';
        d.steps[2] = { key: 'validity', label: '③ 有效性', state: 'err', text: '站点校验失败：Cookie 可能过期 / 平台限流 / 链接本身不可解析' };
      }
    }
    return d;
  }

  /** 页眉按钮显示当前状态 */
  function syncHeaderState() {
    var saved = localStorage.getItem(KEY) || '';
    var sum = quickSummary(saved);
    var btns = document.querySelectorAll('[data-action="cookie"]');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var isFooterLink = b.tagName === 'A';
      if (!isFooterLink) {
        b.classList.toggle('is-on', sum.count > 0);
        var label = b.querySelector('.cookie-label');
        if (label) {
          label.textContent = sum.count > 0 ? 'Cookie 已导入' : '导入 Cookie';
        } else if (b.dataset.action === 'cookie' && b.classList.contains('btn')) {
          b.setAttribute('data-state', sum.count > 0 ? 'on' : 'off');
        }
      }
    }
    var dots = document.querySelectorAll('.header-cookie-dot');
    for (var j = 0; j < dots.length; j++) dots[j].hidden = sum.count === 0;
  }

  /* ---------------- 弹窗 ---------------- */

  function openCookieModal(presetUrl) {
    if (document.getElementById('vgCookieMask')) return;
    var saved = localStorage.getItem(KEY) || '';

    var hintsHtml = PLATFORM_HINTS.map(function (p, i) {
      return (
        '<li><b>' + esc(p.name) + '</b>' +
        '<span>' + esc(p.note) + '</span>' +
        '<code>' + esc(p.key) + '</code>' +
        '<a href="' + p.href + '" target="_blank" rel="noopener">去登录 →</a></li>'
      );
    }).join('');

    var mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.id = 'vgCookieMask';
    mask.innerHTML =
      '<div class="modal modal-wide" role="dialog" aria-modal="true" aria-labelledby="vgCookieTitle">' +
        '<h3 id="vgCookieTitle">导入浏览器 Cookie</h3>' +
        '<div class="modal-scroll">' +
        '<p class="cookie-intro">小红书等平台要求「已登录」的 Cookie 才会返回播放地址；抖音已免 Cookie 支持。导入一次后，本机的解析和下载都会自动带上它。</p>' +

        '<ol class="cookie-steps">' +
          '<li><b>打开站点并登录</b><span>用你平时登录的浏览器打开下面的站点（不用退出登录）</span></li>' +
          '<li><b>复制 Cookie</b><span>按 <kbd>F12</kbd> → Network 面板 → 刷新页面 → 点任意一条请求 → 在 Request Headers 里找到 <code>Cookie</code> → 复制整段值；也可以在 Application → Cookies 里只复制某几行（B 站只要 <code>SESSDATA</code> 一行即可），带 TAB、带引号、带表头都能识别</span></li>' +
          '<li><b>粘贴并验证</b><span>粘到下面，点「验证」，通过后点「保存」</span></li>' +
        '</ol>' +

        '<ul class="cookie-hints">' + hintsHtml + '</ul>' +

        '<div class="cookie-quick" id="vgCookieQuick">' +
          '<div class="cookie-quick-title">最快的方式：只粘值，不用管格式</div>' +
          '<p class="cookie-quick-desc">把浏览器里复制到的<b>那一串值</b>粘进小框，选好平台，点「生成 Cookie」—— 我们自动拼成标准格式。不用管 Tab、引号、表头，也不用照抄下面的示例。</p>' +
          '<div class="cookie-quick-row">' +
            '<label>平台<select id="vgCookiePlatform">' +
              '<option value="SESSDATA">B 站 · SESSDATA</option>' +
              '<option value="web_session">小红书 · web_session</option>' +
              '<option value="sessionid">抖音 · sessionid</option>' +
              '<option value="__custom__">其它 · 自己填名称</option>' +
            '</select></label>' +
            '<label>名称<input id="vgCookieName" type="text" value="SESSDATA" spellcheck="false" readonly /></label>' +
            '<label class="cookie-quick-value">把复制到的值粘这里' +
              '<input id="vgCookieValue" type="text" spellcheck="false" placeholder="例如 fc383b1e,1801033138,..." />' +
            '</label>' +
            '<button type="button" class="btn btn-sm btn-primary" data-act="quickfill">生成 Cookie</button>' +
          '</div>' +
          '<div class="cookie-quick-msg" id="vgCookieQuickMsg"></div>' +
        '</div>' +

        '<label class="cookie-label-input" for="vgCookieText">粘贴 Cookie</label>' +
        '<textarea id="vgCookieText" spellcheck="false" placeholder="a=b; c=d; ...（也支持整行 Cookie: ...、DevTools 表格复制的「名称+值」、Netscape cookie.txt、扩展导出的 JSON）"></textarea>' +
        '<div class="cookie-meta" id="vgCookieMeta">还没有粘贴内容</div>' +

        '<div class="cookie-diag" id="vgCookieDiag">' +
          '<div class="cookie-diag-row" data-step="format"><i></i><b>① 格式识别</b><span></span></div>' +
          '<div class="cookie-diag-row" data-step="login"><i></i><b>② 登录态字段</b><span></span></div>' +
          '<div class="cookie-diag-row" data-step="validity"><i></i><b>③ 有效性</b><span></span></div>' +
        '</div>' +

        '<details class="cookie-examples" id="vgCookieExamples" open>' +
          '<summary>格式示例（默认展开）：4 种能识别的写法 + 常见错误</summary>' +
          '<div class="cookie-examples-body">' +
            '<p class="cookie-ex-title"><b>✓ 能识别（任选一种；B 站只粘 SESSDATA 一行也够）</b></p>' +
            '<pre>' + esc(
              '① 请求头整段（最推荐）\n' +
              '   SESSDATA=xxxxxxxx; bili_jct=yyyy; buvid3=zzzz\n\n' +
              '② DevTools → Application → Cookies 里复制的一行\n' +
              '   SESSDATA⇥"xxxxxxxx"\n' +
              '   ⇥ 代表 Tab 键；值两边的引号可以留着，会自动去掉\n\n' +
              '③ 带前缀的整行\n' +
              '   Cookie: SESSDATA=xxxxxxxx; bili_jct=yyyy\n\n' +
              '④ Netscape cookie.txt / 扩展导出的 JSON\n' +
              '   .bilibili.com⇥TRUE⇥/⇥FALSE⇥0⇥SESSDATA⇥xxxxxxxx\n' +
              '   [{"name":"SESSDATA","value":"xxxxxxxx"}]'
            ) + '</pre>' +
            '<p class="cookie-ex-title bad"><b>✗ 识别不了</b></p>' +
            '<pre>' + esc(
              '· 只复制了值、没带名称：fc383b1e,1801033138,...\n' +
              '· 整页 HTML、cURL 命令、截图文字\n' +
              '· 名称写成中文，或值被换行截断'
            ) + '</pre>' +
            '<p class="cookie-ex-note">示例里的 xxxxxxxx 代表「你复制到的那串值」，不要照抄。带表头、带引号、带 Tab、多列都能识别；<b>最省事的是用上面的「只粘值」生成</b>。</p>' +
          '</div>' +
        '</details>' +

        '<label class="cookie-label-input" for="vgCookieUrl" style="margin-top:12px">用它验证一个链接 <small>（可选，但只有真解析一次才能确认 Cookie 有效）</small></label>' +
        '<input id="vgCookieUrl" class="cookie-url" type="text" spellcheck="false" placeholder="https://www.douyin.com/video/xxxxx" />' +

        '<div class="cookie-result" id="vgCookieResult" hidden></div>' +
        '<p class="cookie-secure">Cookie 只存在你自己的浏览器里，随请求发给本机服务使用，不会上传到任何第三方。</p>' +
        '</div>' +

        '<div class="modal-foot">' +
          '<button type="button" class="btn btn-sm" data-act="cancel">取消</button>' +
          '<button type="button" class="btn btn-sm btn-danger" data-act="clear">清除已存</button>' +
          '<button type="button" class="btn btn-sm" data-act="verify">验证</button>' +
          '<button type="button" class="btn btn-sm btn-primary" data-act="save">保存</button>' +
        '</div>' +
      '</div>';

    document.body.appendChild(mask);

    var ta = mask.querySelector('#vgCookieText');
    var urlInp = mask.querySelector('#vgCookieUrl');
    var meta = mask.querySelector('#vgCookieMeta');
    var diagBox = mask.querySelector('#vgCookieDiag');
    var examples = mask.querySelector('#vgCookieExamples');
    var resultBox = mask.querySelector('#vgCookieResult');
    var verifyBtn = mask.querySelector('[data-act="verify"]');
    var quickBox = mask.querySelector('#vgCookieQuick');
    var platformSel = mask.querySelector('#vgCookiePlatform');
    var nameInp = mask.querySelector('#vgCookieName');
    var valInp = mask.querySelector('#vgCookieValue');
    var quickMsg = mask.querySelector('#vgCookieQuickMsg');
    var lastVerify = null;

    ta.value = saved;
    if (presetUrl) urlInp.value = presetUrl;

    platformSel.addEventListener('change', function () {
      if (platformSel.value === '__custom__') {
        nameInp.readOnly = false;
        nameInp.focus();
      } else {
        nameInp.value = platformSel.value;
        nameInp.readOnly = true;
      }
    });
    valInp.addEventListener('input', function () { valInp.dataset.auto = ''; });

    function refreshMeta() {
      var d = cookieDiagnosis(ta.value, lastVerify);
      meta.className = 'cookie-meta ' + (d.tone || '');
      meta.textContent = d.headline;
      var rows = diagBox.querySelectorAll('.cookie-diag-row');
      for (var i = 0; i < rows.length; i++) {
        var step = d.steps[i];
        rows[i].className = 'cookie-diag-row ' + (step.state || 'idle');
        rows[i].querySelector('span').textContent = step.text;
      }
      if (d.showExamples) examples.open = true;
      // 格式失败时把用户粘的内容搬进「只粘值」小框：点一下「生成 Cookie」就能修好
      if (d.steps[0].state === 'err' && (ta.value || '').trim()) {
        if (!valInp.value.trim() || valInp.dataset.auto === '1') {
          valInp.value = (ta.value || '').trim();
          valInp.dataset.auto = '1';
        }
        quickBox.classList.add('is-highlight');
        quickMsg.className = 'cookie-quick-msg err';
        quickMsg.textContent = '下面这串没识别出格式 —— 已经帮你填进上面的小框了，点「生成 Cookie」即可修好。';
      } else {
        quickBox.classList.remove('is-highlight');
        if (valInp.dataset.auto === '1') {
          valInp.value = '';
          valInp.dataset.auto = '';
        }
        if (quickMsg.className.indexOf('err') >= 0) {
          quickMsg.textContent = '';
          quickMsg.className = 'cookie-quick-msg';
        }
      }
    }
    refreshMeta();
    ta.addEventListener('input', function () {
      lastVerify = null;
      refreshMeta();
    });

    function showResult(kind, html) {
      resultBox.hidden = false;
      resultBox.className = 'cookie-result ' + kind;
      resultBox.innerHTML = html;
    }

    function close() {
      mask.remove();
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) {
      if (e.key === 'Escape') close();
    }

    /* 验证：真解析一次 */
    function doVerify() {
      var text = (ta.value || '').trim();
      if (!text) {
        showResult('err', '请先粘贴 Cookie');
        return;
      }
      verifyBtn.disabled = true;
      verifyBtn.textContent = '验证中…';
      showResult('info', '正在校验' + (urlInp.value.trim() ? '并解析你填写的链接，最多等 45 秒…' : ' Cookie 字段…'));
      var vRow = diagBox.querySelector('[data-step="validity"]');
      vRow.className = 'cookie-diag-row idle';
      vRow.querySelector('span').textContent = '正在校验…';

      fetch('/api/cookies/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookies: text, url: urlInp.value.trim() })
      })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.success) throw new Error((j.detail && String(j.detail)) || '校验失败');
          var d = j.data || {};
          lastVerify = d;
          refreshMeta();
          var probe = d.probe || {};
          var withCookie = probe.with_cookie || {};
          var anon = probe.anonymous || {};

          if (d.ok && probe.with_cookie) {
            var diff = '';
            if (probe.need_cookie) {
              diff = '<div class="cookie-diff">对比：不带 Cookie 时解析失败（' + esc(anon.error || '需要 Cookie') +
                '），带上你的 Cookie 后成功 —— <b>这份 Cookie 是有效的</b></div>';
            } else {
              diff = '<div class="cookie-diff">这条链接本身不需要 Cookie 也能解析（' +
                esc(anon.title || '') + '），所以无法据此判断登录态；换个需要登录的链接再验证会更准。</div>';
            }
            showResult('ok',
              '<b>✓ ' + esc(d.message) + '</b>' +
              (withCookie.title ? '<div>标题：' + esc(withCookie.title) + '</div>' : '') +
              (withCookie.uploader ? '<div>作者：' + esc(withCookie.uploader) + '</div>' : '') +
              diff +
              '<div class="cookie-tip">确认无误后点「保存」，之后解析/下载会自动带上。</div>');
          } else if (d.ok) {
            showResult('ok',
              '<b>✓ ' + esc(d.message) + '</b>' +
              (d.hint ? '<div class="cookie-tip">' + esc(d.hint) + '</div>' : '') +
              '<div class="cookie-tip">点「保存」后，解析和下载会自动带上这份 Cookie。</div>');
          } else {
            // 三类失败给三套完全不同的自救指引，不再笼统说「检查 Cookie」
            var stage = d.stage || '';
            var titles = {
              empty: '格式问题：没识别出任何 Cookie 项',
              anonymous: 'Cookie 内容问题：缺登录态字段',
              auth: '有效性问题：站点没接受这份 Cookie'
            };
            var actions = {
              empty: '不用换 Cookie，请对照下方的「4 种能识别的格式」修改：至少要有「名称=值」；只粘一串值没有名称，识别不了。',
              anonymous: '格式是对的，不用改格式。请登录目标站点后重新复制完整 Cookie，确认包含 SESSDATA（B 站）/ web_session（小红书）/ sessionid（抖音）。',
              auth: '格式和登录态都正常，不用改格式。多半是 Cookie 已过期或失效：重新登录后再复制一次；也可能是平台临时限流，稍后重试。'
            };
            var s2 = d.summary || {};
            showResult('err',
              '<b>✗ ' + esc(titles[stage] || d.message || '校验未通过') + '</b>' +
              (d.message ? '<div>服务端结论：' + esc(d.message) + '</div>' : '') +
              (s2.count ? '<div class="cookie-tip">已识别 ' + s2.count + ' 项（' + esc(formatLabel(s2.format)) + '）' +
                (s2.has_login ? '，含登录态字段' : '，无登录态字段') + '</div>' : '') +
              (d.hint ? '<div class="cookie-tip">' + esc(d.hint) + '</div>' : '') +
              '<div class="cookie-tip"><b>下一步：</b>' + esc(actions[stage] || '检查网络或换个链接重试') + '</div>');
          }
        })
        .catch(function (e) {
          showResult('err', '<b>✗ 校验请求失败</b><div>' + esc(e.message || e) + '</div>');
        })
        .then(function () {
          verifyBtn.disabled = false;
          verifyBtn.textContent = '验证';
        });
    }
    verifyBtn.addEventListener('click', doVerify);

    mask.addEventListener('click', function (e) {
      var act = e.target.getAttribute && e.target.getAttribute('data-act');
      if (e.target === mask || act === 'cancel') return close();

      if (act === 'quickfill') {
        var built = buildCookieFromValue(
          platformSel.value === '__custom__' ? nameInp.value : platformSel.value, valInp.value);
        if (!built.ok) {
          quickMsg.className = 'cookie-quick-msg err';
          quickMsg.textContent = built.error;
          valInp.focus();
          return;
        }
        ta.value = built.text;
        lastVerify = null;
        resultBox.hidden = true;
        refreshMeta();
        quickMsg.className = 'cookie-quick-msg ok';
        quickMsg.textContent = '已生成 ' + built.name + '=「你粘的值」。下面三步诊断应该都变绿了，再点「验证」确认。';
        ta.focus();
        return;
      }

      if (act === 'clear') {
        localStorage.removeItem(KEY);
        ta.value = '';
        lastVerify = null;
        refreshMeta();
        resultBox.hidden = true;
        syncHeaderState();
        return toast('已清除本机保存的 Cookie', 'ok');
      }

      if (act === 'save') {
        var v = (ta.value || '').trim();
        if (!v) {
          localStorage.removeItem(KEY);
          syncHeaderState();
          close();
          return toast('已清除本机保存的 Cookie', 'ok');
        }
        var s3 = quickSummary(v);
        localStorage.setItem(KEY, v);
        syncHeaderState();
        close();
        // 提示统一交给 app.js 发一条：以前这里和 __vgAfterCookieSaved 各发一条，
        // 格式不对时还会出现「已保存」与「没识别出来」自相矛盾的两条提示。
        if (window.__vgAfterCookieSaved) {
          window.__vgAfterCookieSaved({ count: s3.count, hasLogin: s3.hasLogin });
        } else {
          toast('Cookie 已保存到本机（' + s3.count + ' 项）', 'ok');
        }
      }
    });

    document.addEventListener('keydown', onKey);
    ta.focus();
  }

  /* ---------------- 绑定 ---------------- */

  function boot() {
    var btns = document.querySelectorAll('[data-action="cookie"]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function (e) {
        e.preventDefault();
        openCookieModal((window.__vgLastUrl && window.__vgLastUrl()) || '');
      });
    }
    syncHeaderState();
  }

  window.__vgOpenCookie = openCookieModal;
  window.__vgCookieSummary = quickSummary;
  window.__vgCookieDiagnosis = cookieDiagnosis;
  window.__vgCookieFormat = detectCookieFormat;
  window.__vgCookieBuild = buildCookieFromValue;
  window.__vgSyncCookieState = syncHeaderState;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();