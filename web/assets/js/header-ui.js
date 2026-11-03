/**
 * 全站共用的「导入 Cookie」流程（首页 + 卡密兑换页）
 *
 *  1. 引导：告诉用户去哪个站登录、从哪复制 Cookie
 *  2. 粘贴：支持 "a=b; c=d"、整行 "Cookie: ..."、Netscape cookie.txt
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

  /** 本地快速统计：粘贴时就能看到「识别到几项 / 有没有登录态字段」 */
  function quickSummary(text) {
    text = (text || '').trim();
    if (!text) return { count: 0, hasLogin: false, loginKeys: [] };
    var pairs = [];
    if (text.indexOf('\t') >= 0) {
      text.split('\n').forEach(function (line) {
        var cols = line.split('\t');
        if (cols.length >= 7) pairs.push(cols[5].trim());
      });
    } else {
      text.replace(/^cookie:\s*/i, '')
        .split(';')
        .forEach(function (p) {
          if (p.indexOf('=') > 0) pairs.push(p.split('=')[0].trim());
        });
    }
    var LOGIN = ['sessionid', 'sessionid_ss', 'sid_tt', 'SESSDATA', 'web_session', 'sid', 'sso_uid'];
    var hit = pairs.filter(function (n) { return LOGIN.indexOf(n) >= 0; });
    return { count: pairs.length, hasLogin: hit.length > 0, loginKeys: hit };
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
          '<li><b>复制 Cookie</b><span>按 <kbd>F12</kbd> → Network 面板 → 刷新页面 → 点任意一条请求 → 在 Request Headers 里找到 <code>Cookie</code> → 复制整段值</span></li>' +
          '<li><b>粘贴并验证</b><span>粘到下面，点「验证」，通过后点「保存」</span></li>' +
        '</ol>' +

        '<ul class="cookie-hints">' + hintsHtml + '</ul>' +

        '<label class="cookie-label-input" for="vgCookieText">粘贴 Cookie</label>' +
        '<textarea id="vgCookieText" spellcheck="false" placeholder="a=b; c=d; ...（也支持整行 Cookie: ...  或 Netscape 格式的 cookie.txt）"></textarea>' +
        '<div class="cookie-meta" id="vgCookieMeta">还没有粘贴内容</div>' +

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
    var resultBox = mask.querySelector('#vgCookieResult');
    var verifyBtn = mask.querySelector('[data-act="verify"]');

    ta.value = saved;
    if (presetUrl) urlInp.value = presetUrl;

    function refreshMeta() {
      var s = quickSummary(ta.value);
      if (!s.count) {
        meta.className = 'cookie-meta';
        meta.textContent = '还没有粘贴内容';
        return;
      }
      if (s.hasLogin) {
        meta.className = 'cookie-meta ok';
        meta.innerHTML = '识别到 <b>' + s.count + '</b> 项 Cookie，其中登录态字段 <b>' +
          esc(s.loginKeys.join(' / ')) + '</b> —— 看起来像已登录的 Cookie';
      } else {
        meta.className = 'cookie-meta warn';
        meta.innerHTML = '识别到 <b>' + s.count + '</b> 项 Cookie，但<b>没有</b>登录态字段（sessionid / SESSDATA / web_session 等）。' +
          '小红书用游客 Cookie 通常解析不了（抖音已免 Cookie 支持）。';
      }
    }
    refreshMeta();
    ta.addEventListener('input', refreshMeta);

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

      fetch('/api/cookies/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookies: text, url: urlInp.value.trim() })
      })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.success) throw new Error((j.detail && String(j.detail)) || '校验失败');
          var d = j.data || {};
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
            showResult('ok', '<b>✓ ' + esc(d.message) + '</b><div class="cookie-tip">建议再填一个小红书视频链接点「验证」，用真实解析结果确认。</div>');
          } else {
            var s2 = d.summary || {};
            showResult('err',
              '<b>✗ ' + esc(d.message) + '</b>' +
              (s2.count ? '<div>已识别 ' + s2.count + ' 项 Cookie' +
                (s2.has_login ? '（含登录态字段）' : '（无登录态字段）') + '</div>' : '') +
              '<div class="cookie-tip">常见原因：①复制的是未登录状态的 Cookie；②复制时漏了后半段；③Cookie 已过期，重新登录后再复制一次。</div>');
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

      if (act === 'clear') {
        localStorage.removeItem(KEY);
        ta.value = '';
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
          return toast('已清除 Cookie', 'ok');
        }
        var s3 = quickSummary(v);
        if (!s3.hasLogin) {
          // 不强制拦截，但明确提示，避免用户以为能用
          toast('已保存，但没有检测到登录态字段，小红书可能仍解析失败', 'error');
        } else {
          toast('Cookie 已保存到本机（' + s3.count + ' 项）', 'ok');
        }
        localStorage.setItem(KEY, v);
        syncHeaderState();
        // 保存后自动带上链接再解析一次，体验连贯
        close();
        if (window.__vgAfterCookieSaved) window.__vgAfterCookieSaved();
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
  window.__vgSyncCookieState = syncHeaderState;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();