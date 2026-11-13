/* ==========================================================================
   AI 视频总结面板（原生 JS，零依赖，零构建）
   2026-11-12 改版：从「点按钮弹出的全屏遮罩」改成「解析结果右栏常驻面板」。
   - 挂载：app.js 解析成功后调用 __vgAiNote.mount(host, url, title)
   - 触发：先只读探测 /api/ai/lookup —— 命中 done 秒开、命中 running 接回 SSE、
           未命中显示「一键生成」空状态；绝不因为「看一眼」就调用模型
   - 放大：同一个面板 DOM 切 fixed 到 960px，不重建、不中断 SSE、不重复扣费
   - 复用：模块级 noteCache + 服务端幂等 + SSE after 断线续接，三层不变
   - 换链接：unmount() 先断 SSE / 清定时器 / 撤放大层，异步回调带 epoch 校验防串台
   - 导出：复制 / .md / Word(.doc) / PDF(打印) / 导图 SVG，全部前端生成
   ========================================================================== */
(function () {
  'use strict';

  var API = '/api/ai';
  var noteCache = {};   // 模块级：不随 mount / unmount 销毁
  var epoch = 0;        // 每次 mount / unmount / 重新生成都递增，旧异步回调自行作废

  // 与后端 normalize_url 同口径：去掉时间戳跳转与跟踪参数，保留 p（分 P 是不同内容）
  var TRACK = ['t', 'spm_id_from', 'vd_source', 'share_source', 'share_medium',
               'unique_k', 'bbid', 'ts', '_t', 'seid', 'timestamp'];

  function freshState(host, url, title) {
    return {
      host: host || null, panel: null,
      url: url || '', title: title || '',
      taskId: '', timer: null, streamWatch: null, result: null, tab: 'note',
      controller: null, probeCtrl: null, meta: null,
      live: { chars: 0, label: '', stage: '' },
      lastSeq: 0, lastPercent: 0, fallback: false, force: false, reused: false,
      expanded: false, ready: true, status: 'idle', epoch: 0, keyHandler: null
    };
  }
  var state = freshState(null, '', '');

  // 共用 header-ui.js 的安全存储：内嵌预览窗口禁用 localStorage 时读内存副本，
  // 否则这里静默读到空串，AI 总结会因为取不到登录态字幕而失败。
  function readCookie() {
    if (window.__vgStore) return window.__vgStore.get('vg_cookie');
    try { return String(localStorage.getItem('vg_cookie') || ''); } catch (e) { return ''; }
  }
  // 只取登录态字段做本地指纹（不存原文、不下发），换 Cookie 必须换缓存键
  function cookieTag() {
    var raw = readCookie();
    var m = /(?:^|[;\s])(?:SESSDATA|sessionid|sessionid_ss|sid_tt|web_session|sid|sso_uid)\s*[=:\t]\s*"?([^";\s]+)/i.exec(raw);
    var v = m ? m[1] : '', h = 5381;
    for (var i = 0; i < v.length; i++) { h = ((h * 33) ^ v.charCodeAt(i)) | 0; }
    return (h >>> 0).toString(36);
  }
  function cookieText() { return readCookie(); }
  function localKey(url) {
    var s = String(url || '').trim().split('#')[0];
    var q = s.indexOf('?');
    if (q >= 0) {
      var base = s.slice(0, q);
      var keep = s.slice(q + 1).split('&').filter(function (kv) {
        var k = String(kv.split('=')[0] || '').toLowerCase();
        return k && TRACK.indexOf(k) < 0 && k.indexOf('utm_') !== 0;
      });
      s = base + (keep.length ? '?' + keep.join('&') : '');
    }
    return s.replace(/\/+$/, '') + '|detailed|' + cookieTag();
  }
  function remember(result, taskId, at) {
    noteCache[localKey(state.url)] = {
      taskId: taskId || '', result: result, at: at || Date.now()
    };
  }
  function ago(at) {
    if (!at) return '';
    var m = Math.max(0, Math.round((Date.now() - at) / 60000));
    return m < 1 ? '刚刚' : (m < 60 ? m + ' 分钟前' : Math.floor(m / 60) + ' 小时前');
  }

  // 所有面板内查询都限定在当前 panel，避免与页面其它区域 / 旧 DOM 串台
  function $(s, el) { return (el || state.panel || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || state.panel || document).querySelectorAll(s)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function clip(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  function toast(msg, type) {
    var root = document.getElementById('toastRoot');
    if (!root) return;
    var d = document.createElement('div');
    d.className = 'toast ' + (type || '');
    d.textContent = msg;
    root.appendChild(d);
    setTimeout(function () { d.remove(); }, 3200);
  }
  function jump(url, sec) {
    var sep = String(url || '').indexOf('?') >= 0 ? '&' : '?';
    return url + sep + 't=' + Math.max(0, parseInt(sec, 10) || 0);
  }
  function safeJump(url, sec) {
    // 只放行 http/https，避免把非法协议拼进 href / SVG
    if (!/^https?:\/\//i.test(String(url || ''))) return '';
    return jump(url, sec);
  }
  function safeName(title, ext) {
    var n = String(title || 'AI 笔记').replace(/[\\/:*?"<>|\r\n\t]+/g, ' ')
      .replace(/\s+/g, ' ').trim().slice(0, 60) || 'AI 笔记';
    return n + ext;
  }
  function download(blob, filename) {
    var a = document.createElement('a');
    var u = URL.createObjectURL(blob);
    a.href = u;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(u); }, 1000);
  }

  /* ---------- 极简 Markdown 渲染（先转义再解析，防 XSS） ---------- */
  function inline(text) {
    var s = esc(text);
    // 只放行 http/https 链接，避免 javascript: 协议
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, function (m, label, href) {
      return '<a href="' + href + '" target="_blank" rel="noopener">' + label + '</a>';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    return s;
  }

  function renderMarkdown(md) {
    var lines = String(md || '').split('\n');
    var out = [], inList = false;
    function closeList() { if (inList) { out.push('</ul>'); inList = false; } }
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim();
      if (!ln) { closeList(); continue; }
      var h = /^(#{1,4})\s+(.*)$/.exec(ln);
      if (h) {
        closeList();
        var lv = Math.min(h[1].length + 1, 5);
        out.push('<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>');
        continue;
      }
      if (/^(---+|\*\*\*+)$/.test(ln)) { closeList(); out.push('<hr>'); continue; }
      if (/^>\s?/.test(ln)) {
        closeList();
        out.push('<blockquote>' + inline(ln.replace(/^>\s?/, '')) + '</blockquote>');
        continue;
      }
      if (/^[-*]\s+/.test(ln)) {
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push('<li>' + inline(ln.replace(/^[-*]\s+/, '')) + '</li>');
        continue;
      }
      closeList();
      out.push('<p>' + inline(ln) + '</p>');
    }
    closeList();
    return out.join('');
  }

  /* ---------- 章节拆解 tab：从 result.outline 前端渲染，零后端改动 ---------- */
  function renderChapters(r) {
    var list = (r && r.outline) || [];
    if (!list.length) return '<p class="ai-chapter-empty">这份笔记没有章节信息，可点「重新生成」重试。</p>';
    return '<div class="ai-chapters">' + list.map(function (c, i) {
      var sec = (typeof c.sec === 'number') ? c.sec : null;
      var time = c.time || (sec != null ? mmss(sec) : '');
      var href = sec != null ? safeJump(r.url, sec) : '';
      var timeHtml = href
        ? '<a class="ai-chapter-time" href="' + esc(href) + '" target="_blank" rel="noopener" title="跳到原片对应时间">' + esc(time) + '</a>'
        : (time ? '<span class="ai-chapter-time">' + esc(time) + '</span>' : '');
      var pts = (c.points || []).map(function (p) {
        var ps = (typeof p.sec === 'number') ? p.sec : null;
        var ph = ps != null ? safeJump(r.url, ps) : '';
        var a = ph
          ? '<a href="' + esc(ph) + '" target="_blank" rel="noopener" title="跳到原片对应时间">' + esc(mmss(ps)) + '</a>'
          : '';
        return '<li>' + a + esc(p.text || '') + '</li>';
      }).join('');
      return '<section class="ai-chapter">' +
        '<div class="ai-chapter-head"><span class="ai-chapter-no">' + (i + 1) + '</span>' +
        '<h4>' + esc(c.title || '章节') + '</h4>' + timeHtml + '</div>' +
        (pts ? '<ul class="ai-chapter-points">' + pts + '</ul>' : '') +
        '</section>';
    }).join('') + '</div>';
  }

  /* ---------- 思维导图：按知识逻辑重组的多层树（不是章节平铺） ---------- */
  function mmss(sec) {
    // 与后端 _mmss 保持一致：笔记里是 01:02，导图里也应是 01:02
    var s = Math.max(0, parseInt(sec, 10) || 0);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), q = s % 60;
    var mm = String(m).padStart(2, '0'), ss = String(q).padStart(2, '0');
    return (h ? h + ':' : '') + mm + ':' + ss;
  }
  function mmTree(result) {
    var r = result || {};
    if (r.mindmap && r.mindmap.branches && r.mindmap.branches.length) {
      function conv(n) {
        return {
          label: String(n.label || ''), brief: String(n.brief || ''),
          sec: (typeof n.sec === 'number' ? n.sec : null),
          children: (n.children || []).map(conv)
        };
      }
      return {
        root: String(r.mindmap.root || r.title || '视频主题'),
        summary: String(r.mindmap.summary || ''),
        branches: r.mindmap.branches.map(conv),
        meta: r.mindmap.meta || { fallback: true }
      };
    }
    // 旧任务 / 导图生成失败：仍然给出至少 3 层的结构示意，绝不退回单层平铺
    var branches = (r.outline || []).slice(0, 5).map(function (c) {
      return {
        label: String(c.title || '章节'), brief: String(c.time || ''),
        sec: (typeof c.sec === 'number' ? c.sec : null),
        children: (c.points || []).slice(0, 3).map(function (p) {
          return { label: String(p.text || '').slice(0, 24), brief: '',
                   sec: (typeof p.sec === 'number' ? p.sec : null), children: [] };
        })
      };
    });
    return { root: String(r.title || '视频主题'), summary: '按笔记结构生成的示意图',
             branches: branches, meta: { fallback: true } };
  }
  function wrapText(text, perLine, maxLines) {
    var s = String(text || ''), out = [];
    for (var i = 0; i < s.length && out.length < maxLines; i += perLine) {
      out.push(s.slice(i, i + perLine));
    }
    if (s.length > perLine * maxLines && out.length) {
      out[out.length - 1] = out[out.length - 1].slice(0, perLine - 1) + '…';
    }
    return out;
  }

  function mindmapSvg(result) {
    var tree = mmTree(result);
    var COL = 262, PAD = 24, GAP = 12;
    var root = { label: tree.root, brief: tree.summary, sec: null, children: tree.branches };
    function boxH(n, d) { return d === 0 ? 72 : (n.brief || n.sec != null ? 62 : 42); }
    function measure(n, d) {
      var kids = n.children || [], own = boxH(n, d) + GAP;
      if (!kids.length) { n._h = Math.max(46, own); return n._h; }
      var sum = 0;
      kids.forEach(function (k) { sum += measure(k, d + 1); });
      n._h = Math.max(own, sum);
      return n._h;
    }
    var totalH = measure(root, 0), maxDepth = 0;
    function scan(n, d) {
      maxDepth = Math.max(maxDepth, d);
      (n.children || []).forEach(function (k) { scan(k, d + 1); });
    }
    scan(root, 0);
    var W = PAD * 2 + COL * (maxDepth + 1), H = PAD * 2 + totalH + 30;
    function place(n, top, d) {
      n._x = PAD + d * COL; n._w = COL - 34; n._d = d;
      n._y = top + n._h / 2;
      var cy = top;
      (n.children || []).forEach(function (k) { place(k, cy, d + 1); cy += k._h; });
    }
    place(root, PAD, 0);

    var PALETTE = [
      { fill: '#1777ff', stroke: '#1777ff', label: '#ffffff', brief: '#dbeafe', line: '#1777ff' },
      { fill: '#eaf3ff', stroke: '#bcd8ff', label: '#0f3d80', brief: '#4a72a8', line: '#8dbcf5' },
      { fill: '#ffffff', stroke: '#dbe7f7', label: '#0f172a', brief: '#64748b', line: '#c3d8f0' },
      { fill: '#f8fafc', stroke: '#e8eef6', label: '#334155', brief: '#94a3b8', line: '#dde6f0' }
    ];
    var p = [];
    // viewBox + width:100%：窄栏里按容器缩放（旧版 min-width:940px 会横向溢出）
    p.push('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="background:#fff">');
    // 连线先画，节点后画，避免线压在卡片上
    function edges(n) {
      (n.children || []).forEach(function (k) {
        var c = PALETTE[Math.min(k._d, 3)];
        var x1 = n._x + n._w, y1 = n._y, x2 = k._x, y2 = k._y, dx = Math.max(18, (x2 - x1) / 2);
        p.push('<path d="M ' + x1 + ' ' + y1 + ' C ' + (x1 + dx) + ' ' + y1 + ', ' +
          (x2 - dx) + ' ' + y2 + ', ' + x2 + ' ' + y2 +
          '" fill="none" stroke="' + c.line + '" stroke-width="' + (k._d === 1 ? 2 : 1.3) + '"/>');
        edges(k);
      });
    }
    edges(root);

    function draw(n) {
      var c = PALETTE[Math.min(n._d, 3)];
      var h = boxH(n, n._d), x = n._x, y = n._y - h / 2, w = n._w;
      var href = n.sec != null ? safeJump(result.url, n.sec) : '';
      if (href) p.push('<a href="' + esc(href) + '" target="_blank" rel="noopener">');
      p.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h +
        '" rx="' + (n._d === 0 ? 16 : 10) + '" fill="' + c.fill + '" stroke="' + c.stroke + '" stroke-width="1"/>');
      if (n._d === 1) {
        p.push('<rect x="' + x + '" y="' + (y + 9) + '" width="3" height="' + (h - 18) +
          '" rx="1.5" fill="#1777ff"/>');
      }
      var labelLines = wrapText(n.label, n._d === 0 ? 11 : 14, 2);
      var lh = n._d === 0 ? 19 : 16;
      var ty = y + (n._d === 0 ? 24 : 21);
      labelLines.forEach(function (ln, i) {
        p.push('<text x="' + (x + 13) + '" y="' + (ty + i * lh) + '" font-size="' +
          (n._d === 0 ? 15 : n._d === 1 ? 13.5 : 12.5) + '" font-weight="' +
          (n._d <= 1 ? 700 : 600) + '" fill="' + c.label + '">' + esc(ln) + '</text>');
      });
      var bLines = wrapText(n.brief, n.sec != null ? 14 : (n._d === 0 ? 15 : 17),
                            n.sec != null ? 1 : 2);
      var by = ty + labelLines.length * lh + 3;
      bLines.forEach(function (ln, i) {
        if (by + i * 14 > y + h - 5) return;
        p.push('<text x="' + (x + 13) + '" y="' + (by + i * 14) +
          '" font-size="10.5" fill="' + c.brief + '">' + esc(ln) + '</text>');
      });
      // 时间码单独画在右下角，避免被 brief 折行从中间切断
      if (n.sec != null) {
        p.push('<text x="' + (x + w - 10) + '" y="' + (y + h - 7) +
          '" text-anchor="end" font-size="9.5" fill="' + (n._d === 0 ? '#dbeafe' : '#1777ff') +
          '">◷ ' + esc(mmss(n.sec)) + '</text>');
      }
      if (href) p.push('</a>');
      (n.children || []).forEach(draw);
    }
    draw(root);
    var note = tree.meta && tree.meta.fallback
      ? '导图模型未返回合格结构，当前为笔记结构示意；请重试以生成按知识逻辑重组的多层导图。'
      : 'AI 按知识逻辑独立重组（非章节复述）· 共 ' + (maxDepth + 1) + ' 层 · 点击带 ◷ 的节点可跳回原片';
    p.push('<text x="' + PAD + '" y="' + (H - 10) + '" font-size="11" fill="#94a3b8">' + esc(note) + '</text>');
    p.push('</svg>');
    return p.join('');
  }

  /* ---------- 导出（全部前端生成，零新增依赖） ---------- */
  function exportBy(act) {
    var r = state.result;
    if (!r) return;
    if (act === 'copy') {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(r.markdown).then(function () { toast('笔记已复制', 'ok'); },
          function () { toast('复制失败，请手动选择文本', 'error'); });
      } else {
        toast('浏览器不支持剪贴板，请手动选择', 'error');
      }
    } else if (act === 'md') {
      download(new Blob([r.markdown], { type: 'text/markdown;charset=utf-8' }), safeName(r.title, '.md'));
    } else if (act === 'word') {
      var html = '<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">' +
        '<head><meta charset="utf-8"><title>' + esc(r.title) + '</title></head><body>' +
        renderMarkdown(r.markdown) + '</body></html>';
      download(new Blob(['\ufeff', html], { type: 'application/msword;charset=utf-8' }), safeName(r.title, '.doc'));
    } else if (act === 'svg') {
      download(new Blob([mindmapSvg(r)], { type: 'image/svg+xml;charset=utf-8' }), safeName(r.title, '-思维导图.svg'));
    } else if (act === 'pdf') {
      ensurePrintHost();
      var host = document.getElementById('aiPrintHost');
      if (!host) return;
      host.innerHTML = renderMarkdown(r.markdown);
      window.print();
    }
  }

  /* ---------- 流式渲染：活体指示 + 增量草稿 ---------- */
  function liveInner() {
    var l = state.live, meta = state.meta, bits = [];
    bits.push('<i class="dot"></i><span>' + esc(l.stage || '正在生成…') + '</span>');
    if (state.reused) {
      bits.push('<span class="ai-live-reused">已接回上次任务，不会重复扣费</span>');
    }
    if (meta) {
      bits.push('<span class="ai-live-meta">' + esc(meta.segments) + ' 段字幕 · 覆盖到 ' +
        esc(meta.subtitle_end_text || '未知') + '</span>');
    }
    if (l.chars) {
      bits.push('<span class="ai-live-meta">模型已输出 ' + l.chars + ' 字' +
        (l.label ? '（' + esc(l.label) + '）' : '') + '</span>');
    }
    return bits.join('');
  }
  function liveBar() { return '<div class="ai-live">' + liveInner() + '</div>'; }

  function updateLive() {
    var el = $('.ai-live');
    if (el) { el.innerHTML = liveInner(); }
    var sub = $('#aiSub');
    if (sub && !state.result) { sub.textContent = state.live.stage || '正在生成…'; }
  }

  function renderDraft(d) {
    var body = $('#aiBody');
    if (!body || !d) return;
    var top = body.scrollTop;
    state.status = 'running';
    state.live.stage = d.stage || state.live.stage || '生成中';
    body.innerHTML = liveBar() + '<article class="ai-md ai-draft">' +
      renderMarkdown(d.markdown) + '</article>';
    body.scrollTop = top;
    var sub = $('#aiSub');
    if (sub) {
      sub.textContent = '生成中 · ' + (d.chapters ? d.chapters.length : 0) + ' 章 / ' +
        (d.total_points || 0) + ' 条要点（完成后可导出）';
    }
  }

  function parseFrame(frame) {
    var name = '', data = {}, id = 0;
    String(frame || '').split('\n').forEach(function (raw) {
      var line = raw.replace(/\r$/, '');
      if (line.indexOf('event: ') === 0) name = line.slice(7).trim();
      else if (line.indexOf('data: ') === 0) {
        try { data = JSON.parse(line.slice(6)); } catch (e) { data = {}; }
      } else if (line.indexOf('id: ') === 0) id = parseInt(line.slice(4), 10) || 0;
    });
    return name ? { name: name, data: data, id: id } : null;
  }

  function handleEvent(ev) {
    var name = ev.name, d = ev.data || {};
    if (name === 'stage') {
      setProgress(d.percent, d.message);
      state.live.stage = d.message || '生成中';
      updateLive();
    } else if (name === 'meta') {
      state.meta = d;
      updateLive();
    } else if (name === 'token') {
      state.live.chars = d.chars || 0;
      state.live.label = d.label || '';
      state.live.stage = '正在生成' + (d.label ? '「' + d.label + '」' : '') + '…';
      updateLive();
    } else if (name === 'partial') {
      renderDraft(d);
    } else if (name === 'done') {
      if (d.result) showResult(d.result);
    } else if (name === 'error') {
      showError(d.message || '总结失败');
    }
  }

  // 内嵌预览窗口 / 中间代理可能缓冲 SSE：fetch 或 reader.read() 长期 pending 时，
  // 没有超时就会永远停在「正在连接实时生成通道…」。看门狗到期自动切轮询兜底。
  // 后端 stream_events 每 15s 发一次 ping 心跳，所以首帧阈值必须大于 15s；
  // first 只覆盖「连响应头都没拿到」，响应头一到就放宽到 idle（心跳会持续续期）。
  var STREAM_TIMEOUT = { first: 20000, idle: 45000 };
  function clearStreamWatch() {
    if (state.streamWatch) { clearTimeout(state.streamWatch); state.streamWatch = null; }
  }

  function fallbackPoll() {
    clearStreamWatch();
    if (state.fallback || !state.taskId || state.result) return;
    state.fallback = true;
    setProgress(state.lastPercent || 5, '实时通道中断，已切换到常规刷新…');
    poll();
  }
  function onStreamClosed() { if (!state.result) fallbackPoll(); }

  function openStream(after) {
    if (!state.taskId) return;
    var ctrl = new AbortController();
    state.controller = ctrl;
    var myEpoch = state.epoch;
    clearStreamWatch();
    function arm(ms) {
      clearStreamWatch();
      state.streamWatch = setTimeout(function () {
        state.streamWatch = null;
        if (state.epoch !== myEpoch || state.result || state.fallback) return;
        setProgress(state.lastPercent || 5, '实时通道无响应，已切换到常规刷新…');
        try { ctrl.abort(); } catch (e) { /* 已结束 */ }
        fallbackPoll();
      }, ms);
    }
    function touch() { arm(STREAM_TIMEOUT.idle); }
    arm(STREAM_TIMEOUT.first);   // 仅覆盖 fetch Promise 一直 pending

    fetch(API + '/summary/' + encodeURIComponent(state.taskId) + '/stream?after=' + (after || 0),
      { signal: ctrl.signal })
      .then(function (res) {
        if (!res.ok || !res.body) throw new Error('流式通道不可用');
        // 响应头已到，说明通道本身是通的；等第一块数据给足 45s（后端 15s 心跳会续期）
        arm(STREAM_TIMEOUT.idle);
        var reader = res.body.getReader();
        var dec = new TextDecoder('utf-8');
        var buf = '';
        function pump() {
          return reader.read().then(function (r) {
            if (state.epoch !== myEpoch) return;
            if (r.done) { clearStreamWatch(); onStreamClosed(); return; }
            touch();                       // 每收到一块数据就续期
            buf += dec.decode(r.value, { stream: true });
            var frames = buf.split('\n\n');
            buf = frames.pop();
            frames.forEach(function (f) {
              if (state.epoch !== myEpoch) return;
              var ev = parseFrame(f);
              if (!ev) return;
              if (ev.id > state.lastSeq) state.lastSeq = ev.id;
              handleEvent(ev);
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (e) {
        clearStreamWatch();
        if (state.epoch !== myEpoch) return;
        if (e && e.name === 'AbortError') return;
        fallbackPoll();
      });
  }

  /* ---------- 面板骨架 ---------- */
  function ensurePrintHost() {
    if (!document.getElementById('aiPrintHost')) {
      var ph = document.createElement('div');
      ph.id = 'aiPrintHost';
      document.body.appendChild(ph);
    }
  }

  function panelClick(e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.closest('#aiExpand')) { setExpanded(true); return; }
    if (t.closest('#aiCollapse')) { setExpanded(false); return; }
    if (t.closest('#aiGenerate')) { startGenerate(false); return; }
    if (t.closest('#aiRetry')) { startGenerate(false); return; }
    var tab = t.closest('.ai-tab');
    if (tab) { state.tab = tab.getAttribute('data-tab'); renderTab(); return; }
    var act = t.closest('[data-act]');
    if (act) {
      var a = act.getAttribute('data-act');
      if (a === 'regen') { startGenerate(true); return; }
      exportBy(a);
    }
  }

  function buildPanel() {
    var host = state.host;
    if (!host) return;
    host.innerHTML =
      '<div class="ai-panel" role="region" aria-label="AI 视频总结">' +
        '<div class="ai-head">' +
          '<div><h3>AI 视频总结</h3><p id="aiSub">准备中…</p></div>' +
          '<div class="ai-head-actions">' +
            '<button class="ai-expand" id="aiExpand" type="button" title="放大到宽屏阅读（不会重新生成）">放大</button>' +
            '<button class="ai-close" id="aiCollapse" type="button" title="收起回右栏" hidden>×</button>' +
          '</div>' +
        '</div>' +
        '<div class="ai-progress" id="aiProgress" hidden>' +
          '<div class="bar"><i id="aiBar"></i></div><span id="aiStage">准备中…</span>' +
        '</div>' +
        '<div class="ai-toolbar" id="aiToolbar" hidden>' +
          '<div class="ai-tabs">' +
            '<button class="ai-tab is-on" type="button" data-tab="note">笔记</button>' +
            '<button class="ai-tab" type="button" data-tab="chapters">章节拆解</button>' +
            '<button class="ai-tab" type="button" data-tab="map">思维导图</button>' +
          '</div>' +
          '<span class="grow"></span>' +
          '<button class="btn btn-sm" type="button" data-act="copy">复制</button>' +
          '<button class="btn btn-sm" type="button" data-act="md">.md</button>' +
          '<button class="btn btn-sm" type="button" data-act="word">Word</button>' +
          '<button class="btn btn-sm" type="button" data-act="pdf">PDF</button>' +
          '<button class="btn btn-sm" type="button" data-act="svg">导图 SVG</button>' +
          '<button class="btn btn-sm ai-regen" type="button" data-act="regen" title="忽略缓存，重新调用模型生成">重新生成</button>' +
        '</div>' +
        '<div class="ai-body" id="aiBody"></div>' +
      '</div>';
    state.panel = $('.ai-panel', host);
    if (state.panel) state.panel.addEventListener('click', panelClick);
  }

  /* 放大 / 收起：同一个 DOM，只切 CSS + 插拔遮罩，SSE / 缓存 / 滚动位置全部保留 */
  function setExpanded(on) {
    on = !!on;
    state.expanded = on;
    if (state.panel) state.panel.classList.toggle('is-expanded', on);
    document.body.classList.toggle('ai-expanded-open', on);
    var bd = document.getElementById('aiBackdrop');
    if (on && !bd) {
      bd = document.createElement('div');
      bd.className = 'ai-backdrop';
      bd.id = 'aiBackdrop';
      bd.addEventListener('click', function () { setExpanded(false); });
      document.body.appendChild(bd);
    } else if (!on && bd) {
      bd.remove();
    }
    var ex = $('#aiExpand'), co = $('#aiCollapse');
    if (ex) ex.hidden = on;
    if (co) co.hidden = !on;
  }

  /* ---------- 面板状态 ---------- */
  function setProgress(percent, text) {
    state.lastPercent = Number(percent) || 0;
    var prog = $('#aiProgress');
    if (!prog) return;
    prog.hidden = false;
    var bar = $('#aiBar');
    if (bar) { bar.style.width = Math.max(2, Number(percent) || 0) + '%'; }
    var stage = $('#aiStage');
    if (stage) { stage.textContent = text || '处理中…'; }
  }
  function hideChrome() {
    var prog = $('#aiProgress'); if (prog) prog.hidden = true;
    var tb = $('#aiToolbar'); if (tb) tb.hidden = true;
  }

  // 把「AI 面板到底有没有读到 Cookie」直接写在空状态里：
  // 内嵌预览窗口存储受限时，用户不用开控制台就知道原因。
  function cookieStateHtml() {
    var raw = cookieText();
    var names = (raw.match(/[A-Za-z_][A-Za-z0-9_.\-]*\s*=/g) || []).length;
    var storageOk = !(window.__vgStore && window.__vgStore.available === false);
    var warn = storageOk ? '' :
      '当前窗口禁用了本机存储：Cookie 仅本次页面有效，刷新后需重新导入；长期建议在外部浏览器打开。';
    var state = raw
      ? '已读取到 Cookie（' + names + ' 项），生成总结时会自动带上。'
      : '未检测到 Cookie：B 站高清 / 小红书等需要登录态字幕，请先点页眉「导入 Cookie」。';
    return '<div class="ai-cookie-state' + (storageOk && raw ? ' ok' : ' warn') + '">' +
      esc(warn) + (warn ? '<br>' : '') + esc(state) + '</div>';
  }

  function showIdle(ready) {
    state.status = 'idle';
    state.ready = ready !== false;
    hideChrome();
    var sub = $('#aiSub');
    if (sub) sub.textContent = '尚未生成 · 点下方按钮开始';
    var body = $('#aiBody');
    if (!body) return;
    body.innerHTML =
      '<div class="ai-idle">' +
        '<div class="ai-idle-ico"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0-3.6 10.8c.6.5.9 1.1 1 1.7h5.2c.1-.6.4-1.2 1-1.7A6 6 0 0 0 12 3Z"/><path d="M9.5 19h5M10 21.5h4"/></svg></div>' +
        '<h4>一键看懂这条视频</h4>' +
        '<p>基于平台官方字幕生成：内容大纲 + 核心要点 + 带时间戳的章节拆解，' +
          '以及按知识逻辑独立重组的多层思维导图。</p>' +
        (state.ready ? '' :
          '<p class="ai-idle-tip">服务端尚未配置 AI 模型（.env 里的 AI_BASE_URL / AI_MODEL）。' +
          '配置并重启服务后即可使用；视频下载不受影响。</p>') +
        '<button class="btn btn-primary btn-sm" type="button" id="aiGenerate"' + (state.ready ? '' : ' disabled') + '>生成 AI 总结</button>' +
        cookieStateHtml() +
        '<div class="ai-idle-meta">仅点击后才会调用模型并产生费用；<br>' +
          '同一视频再次打开会直接复用已有笔记，不会重复扣费。<br>' +
          '文案来自平台字幕，无字幕的视频无法生成总结。</div>' +
      '</div>';
  }

  function showError(msg) {
    clearStreamWatch();
    state.status = 'error';
    hideChrome();
    var sub = $('#aiSub'); if (sub) sub.textContent = '总结未完成';
    var body = $('#aiBody');
    if (!body) return;
    var err = '<div class="ai-error"><b>没能生成完整笔记</b><p>' + esc(msg) +
      '</p><button class="btn btn-sm" type="button" id="aiRetry">重试</button></div>';
    // 已经长出来的草稿不丢，错误追加在后面，用户不会白等一场
    if ($('.ai-draft', body)) body.insertAdjacentHTML('beforeend', err);
    else body.innerHTML = err;
  }

  function renderTab() {
    var r = state.result, body = $('#aiBody');
    if (!r || !body) return;
    $$('.ai-tab').forEach(function (b) {
      b.classList.toggle('is-on', b.getAttribute('data-tab') === state.tab);
    });
    if (state.tab === 'map') {
      body.innerHTML = '<div class="ai-mindmap">' + mindmapSvg(r) + '</div>' +
        '<p class="ai-tip">导图由 AI 按知识逻辑独立重组，不是把笔记章节换个位置摆：' +
        '从核心概念到因果机制、例证与延伸逐层展开。带 ◷ 的节点可跳回原片对应时间；' +
        '窄屏若看不清，可点右上角「放大」。</p>';
    } else if (state.tab === 'chapters') {
      body.innerHTML = renderChapters(r) +
        '<p class="ai-tip">点击蓝色时间码可跳回原片对应位置；章节与要点来自同一份笔记，不额外调用模型。</p>';
    } else {
      body.innerHTML = '<article class="ai-md">' + renderMarkdown(r.markdown) + '</article>';
    }
    body.scrollTop = 0;
  }

  function showResult(result, opts) {
    if (!result) return;
    clearStreamWatch();
    state.status = 'done';
    state.result = result;
    remember(result, state.taskId, opts && opts.at);
    hideChrome();
    var tb = $('#aiToolbar'); if (tb) tb.hidden = false;
    var sub = $('#aiSub');
    if (sub) {
      var s = result.stats || {};
      var tail = (opts && opts.cached)
        ? ' · 缓存结果' + (opts.at ? '（生成于 ' + ago(opts.at) + '）' : '') + '，点「重新生成」可重算'
        : '';
      sub.textContent = (s.chunks || '?') + ' 段字幕 · ' + (s.llm_calls || '?') +
        ' 次 AI 调用 · ' + ((result.outline || []).length) + ' 章 / 导图 ' +
        (s.map_depth || '?') + ' 层 · ' + (s.provider || 'AI') + tail;
    }
    state.tab = 'note';
    renderTab();
  }

  /* ---------- 提交 / 探测 / 轮询 ---------- */
  function resetRuntime() {
    clearTimeout(state.timer); state.timer = null;
    clearStreamWatch();
    if (state.controller) { try { state.controller.abort(); } catch (e) { /* 已结束 */ } }
    state.controller = null;
    if (state.probeCtrl) { try { state.probeCtrl.abort(); } catch (e) { /* 已结束 */ } }
    state.probeCtrl = null;
    epoch += 1; state.epoch = epoch;
    state.taskId = ''; state.result = null; state.tab = 'note'; state.meta = null;
    state.live = { chars: 0, label: '', stage: '' };
    state.lastSeq = 0; state.lastPercent = 0; state.fallback = false;
    state.force = false; state.reused = false; state.status = 'running';
  }

  function startGenerate(force) {
    resetRuntime();
    state.force = !!force;
    var prog = $('#aiProgress'); if (prog) prog.hidden = false;
    var tb = $('#aiToolbar'); if (tb) tb.hidden = true;
    var sub = $('#aiSub'); if (sub) sub.textContent = force ? '正在重新生成…' : '正在提交…';
    var body = $('#aiBody');
    if (body) body.innerHTML = liveBar() + '<div class="ai-loading">正在连接实时生成通道…</div>';
    submit();
  }

  // 只读探测：不创建任务、不调模型、不落临时 Cookie 文件
  function probe() {
    var ctrl = new AbortController();
    state.probeCtrl = ctrl;
    var myEpoch = state.epoch;
    var sub = $('#aiSub'); if (sub) sub.textContent = '正在检查已有笔记…';
    var body = $('#aiBody');
    if (body) body.innerHTML = '<div class="ai-loading">正在检查已有笔记…</div>';
    fetch(API + '/lookup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: state.url, cookies: cookieText(), style: 'detailed' }),
      signal: ctrl.signal
    }).then(function (res) {
      return res.json().then(function (b) {
        if (!res.ok) throw new Error(b.detail || ('请求失败（' + res.status + '）'));
        return b;
      });
    }).then(function (b) {
      if (state.epoch !== myEpoch) return;
      var d = (b && b.data) || {};
      if (d.found && d.status === 'done' && d.result) {
        state.taskId = d.id || '';
        showResult(d.result, { cached: true, at: d.at_ms });
      } else if (d.found && (d.status === 'queued' || d.status === 'running')) {
        state.taskId = d.id || '';
        state.reused = true;
        state.status = 'running';
        setProgress(d.percent || 5, d.message || '已接回正在进行的生成…');
        var bd = $('#aiBody');
        if (bd) bd.innerHTML = liveBar() + '<div class="ai-loading">正在接回生成进度…</div>';
        if (d.partial) renderDraft(d.partial);
        openStream(0);
      } else {
        showIdle(d.summarizer_ready !== false);
      }
    }).catch(function (e) {
      if (state.epoch !== myEpoch || (e && e.name === 'AbortError')) return;
      // 探测失败不能把右栏变成死区：降级成「一键生成」，由真正提交时再报错
      showIdle(true);
    });
  }

  function submit() {
    var myEpoch = state.epoch;
    setProgress(2, '正在提交 AI 总结任务…');
    fetch(API + '/summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: state.url,
        cookies: cookieText(),
        style: 'detailed',
        force: !!state.force
      })
    }).then(function (res) {
      return res.json().then(function (b) {
        if (!res.ok) throw new Error(b.detail || ('请求失败（' + res.status + '）'));
        return b;
      });
    }).then(function (b) {
      if (state.epoch !== myEpoch) return;
      var d = b.data || {};
      state.taskId = d.id || '';
      state.reused = !!d.reused;
      // 后端幂等：已有结果直接秒开，0 次模型调用
      if (d.status === 'done' && d.result) { showResult(d.result, { cached: !!d.reused }); return; }
      if (d.reused) setProgress(d.percent || 10, '已接回上次未完成的生成…');
      if (d.partial && !state.result) renderDraft(d.partial);
      openStream(0);
    }).catch(function (e) {
      if (state.epoch !== myEpoch) return;
      showError(e.message || '提交失败');
    });
  }

  function poll() {
    clearTimeout(state.timer);
    var myEpoch = state.epoch;
    function tick() {
      if (state.epoch !== myEpoch) return;
      fetch(API + '/summary/' + encodeURIComponent(state.taskId))
        .then(function (res) { return res.json(); })
        .then(function (b) {
          if (state.epoch !== myEpoch) return;
          if (!b || !b.success) throw new Error('查询任务失败');
          var t = b.data || {};
          setProgress(t.percent, t.message);
          if (t.partial && !state.result) renderDraft(t.partial);
          if (t.status === 'done' && t.result) { showResult(t.result); return; }
          if (t.status === 'error') { showError(t.message || '总结失败'); return; }
          state.timer = setTimeout(tick, 1000);
        }).catch(function (e) {
          if (state.epoch !== myEpoch) return;
          showError(e.message || '网络异常');
        });
    }
    tick();
  }

  /* ---------- 生命周期 ---------- */
  function mount(host, url, title) {
    if (!host || !url) return;
    if (state.host === host && state.url === url && state.panel && state.panel.isConnected !== false) return;
    unmount();
    epoch += 1;
    state = freshState(host, url, title || '');
    state.epoch = epoch;
    ensurePrintHost();
    buildPanel();
    state.keyHandler = function (e) {
      if (e.key === 'Escape' && state.expanded) setExpanded(false);
    };
    document.addEventListener('keydown', state.keyHandler);

    // 本地缓存最优先：换页回来 0 请求秒开
    var cached = noteCache[localKey(url)];
    if (cached && cached.result) {
      state.taskId = cached.taskId || '';
      showResult(cached.result, { cached: true, at: cached.at });
    } else {
      probe();
    }
  }

  function unmount() {
    clearTimeout(state.timer); state.timer = null;
    clearStreamWatch();
    if (state.controller) { try { state.controller.abort(); } catch (e) { /* 已结束 */ } }
    if (state.probeCtrl) { try { state.probeCtrl.abort(); } catch (e) { /* 已结束 */ } }
    if (state.keyHandler) document.removeEventListener('keydown', state.keyHandler);
    epoch += 1;
    setExpanded(false);          // 撤遮罩 + 解 body 滚动锁，必须早于清 DOM
    var h = state.host;
    if (h) h.innerHTML = '';     // 只清右栏，绝不动左栏下载区
    state = freshState(null, '', '');
    state.epoch = epoch;
  }

  // 兼容旧入口：万一还有页面残留「AI 总结」按钮，就放大右栏而不是静默失败
  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest && e.target.closest('#aiNoteBtn');
    if (!btn) return;
    if (state.panel && state.panel.isConnected !== false) setExpanded(true);
    else toast('请先解析一个视频', 'error');
  });

  // 导出纯函数 / 生命周期给自测（线上逻辑不依赖）
  window.__vgAiNote = {
    mount: mount,
    unmount: unmount,
    timeouts: STREAM_TIMEOUT,   // 自测可临时调小，验证中间层缓冲 SSE 时的降级
    regen: function () { startGenerate(true); },
    generate: function () { startGenerate(false); },
    setExpanded: setExpanded,
    showIdle: showIdle,
    showResult: showResult,
    renderChapters: renderChapters,
    renderMarkdown: renderMarkdown,
    mindmapSvg: mindmapSvg,
    mindmapTree: mmTree,
    mmss: mmss,
    wrapText: wrapText,
    safeName: safeName,
    jump: jump,
    parseFrame: parseFrame,
    liveInner: liveInner,
    freshState: freshState,
    handleEvent: handleEvent,
    localKey: localKey,
    ago: ago,
    clip: clip,
    getCache: function () { return noteCache; },
    getState: function () { return state; }
  };
})();
