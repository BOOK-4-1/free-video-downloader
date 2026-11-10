/* ==========================================================================
   AI 视频总结面板（原生 JS，零依赖，零构建）
   入口：解析结果卡里的 #aiNoteBtn（事件委托，重新解析后依然有效）
   流程：POST /api/ai/summary → SSE 读 /api/ai/summary/{id}/stream → 增量渲染
   生成中：笔记逐章长出 + 活体指示；工具栏保持隐藏，收到 done 才解锁导出
   断流：自动回退到 1 秒轮询（轮询结果里同样带 partial 草稿）
   复用：同一视频的笔记缓存在模块级 noteCache，关面板 / 去下载 / 切页回来
         直接秒开；生成中回来用同一个 task_id 续接 SSE，不重复调用模型
   导出：复制 / .md / Word(.doc) / PDF(打印) / 导图 SVG，全部前端生成
   ========================================================================== */
(function () {
  'use strict';

  var API = '/api/ai';
  var noteCache = {};   // 模块级：不随 openPanel / close 销毁
  function freshState(url) {
    return { taskId: '', timer: null, result: null, tab: 'note', url: url || '',
             controller: null, meta: null, live: { chars: 0, label: '', stage: '' },
             lastSeq: 0, lastPercent: 0, fallback: false, force: false, reused: false };
  }
  var state = freshState('');

  // 与后端 normalize_url 同口径：去掉时间戳跳转与跟踪参数，保留 p（分 P 是不同内容）
  var TRACK = ['t', 'spm_id_from', 'vd_source', 'share_source', 'share_medium',
               'unique_k', 'bbid', 'ts', '_t', 'seid', 'timestamp'];
  // 只取登录态字段做本地指纹（不存原文、不下发），换 Cookie 必须换缓存键
  function cookieTag() {
    var raw = '';
    try { raw = String(localStorage.getItem('vg_cookie') || ''); } catch (e) { raw = ''; }
    var m = /(?:^|[;\s])(?:SESSDATA|sessionid|sessionid_ss|sid_tt|web_session|sid|sso_uid)\s*[=:\t]\s*"?([^";\s]+)/i.exec(raw);
    var v = m ? m[1] : '', h = 5381;
    for (var i = 0; i < v.length; i++) { h = ((h * 33) ^ v.charCodeAt(i)) | 0; }
    return (h >>> 0).toString(36);
  }
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

  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
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

  /* ---------- 思维导图：按知识逻辑重组的多层树（不是章节平铺） ---------- */
  function mmss(sec) {
    // 与后端 _mmss 保持一致：笔记里是 01:02，导图里也应是 01:02
    var s = Math.max(0, parseInt(sec, 10) || 0);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), q = s % 60;
    var mm = String(m).padStart(2, '0'), ss = String(q).padStart(2, '0');
    return (h ? h + ':' : '') + mm + ':' + ss;
  }
  function safeJump(url, sec) {
    // 只放行 http/https，避免把非法协议拼进 SVG 的 href
    if (!/^https?:\/\//i.test(String(url || ''))) return '';
    return jump(url, sec);
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
      bits.push('<span class="ai-live-meta">' + meta.segments + ' 段字幕 · 覆盖到 ' +
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
      state.result = d.result;
      showResult(d.result);
    } else if (name === 'error') {
      showError(d.message || '总结失败');
    }
  }

  function fallbackPoll() {
    if (state.fallback || !state.taskId || state.result) return;
    state.fallback = true;
    setProgress(state.lastPercent || 5, '实时通道中断，已切换到常规刷新…');
    poll();
  }

  function onStreamClosed() { if (!state.result) fallbackPoll(); }

  function openStream(after) {
    var ctrl = new AbortController();
    state.controller = ctrl;
    fetch(API + '/summary/' + encodeURIComponent(state.taskId) + '/stream?after=' + (after || 0),
      { signal: ctrl.signal })
      .then(function (res) {
        if (!res.ok || !res.body) throw new Error('流式通道不可用');
        var reader = res.body.getReader();
        var dec = new TextDecoder('utf-8');
        var buf = '';
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) { onStreamClosed(); return; }
            buf += dec.decode(r.value, { stream: true });
            var frames = buf.split('\n\n');
            buf = frames.pop();
            frames.forEach(function (f) {
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
        if (e && e.name === 'AbortError') return;
        fallbackPoll();
      });
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

  function showError(msg) {
    var prog = $('#aiProgress'); if (prog) { prog.hidden = true; }
    var tb = $('#aiToolbar'); if (tb) { tb.hidden = true; }
    var sub = $('#aiSub'); if (sub) { sub.textContent = '总结未完成'; }
    var body = $('#aiBody');
    if (!body) return;
    var err = '<div class="ai-error"><b>没能生成完整笔记</b><p>' + esc(msg) +
      '</p><button class="btn btn-sm" id="aiRetry">重试</button></div>';
    // 已经长出来的草稿不丢，错误追加在后面，用户不会白等一场
    if ($('.ai-draft', body)) { body.insertAdjacentHTML('beforeend', err); }
    else { body.innerHTML = err; }
    var retry = $('#aiRetry', body);
    if (retry) {
      retry.onclick = function () {
        var u = state.url;
        state = freshState(u);
        body.innerHTML = liveBar() + '<div class="ai-loading">正在重新提交…</div>';
        submit();
      };
    }
  }

  function renderTab() {
    var r = state.result, body = $('#aiBody');
    if (!r || !body) return;
    $$('.ai-tab').forEach(function (b) { b.classList.toggle('is-on', b.getAttribute('data-tab') === state.tab); });
    if (state.tab === 'map') {
      body.innerHTML = '<div class="ai-mindmap">' + mindmapSvg(r) + '</div>' +
        '<p class="ai-tip">导图由 AI 按知识逻辑独立重组，不是把笔记章节换个位置摆：' +
        '从核心概念到因果机制、例证与延伸逐层展开。带 ◷ 的节点可跳回原片对应时间；' +
        '完整要点请切回「笔记」。</p>';
    } else {
      body.innerHTML = '<article class="ai-md">' + renderMarkdown(r.markdown) + '</article>';
    }
  }

  function showResult(result, opts) {
    state.result = result;
    var cached = !!(opts && opts.cached);
    remember(result, state.taskId, opts && opts.at);
    var prog = $('#aiProgress'); if (prog) { prog.hidden = true; }
    var tb = $('#aiToolbar'); if (tb) { tb.hidden = false; }
    var sub = $('#aiSub');
    if (sub) {
      var tail = cached ? ' · 缓存结果' + (opts && opts.at ? '（生成于 ' + ago(opts.at) + '）' : '') +
        '，点「重新生成」可重算' : '';
      sub.textContent = result.stats.chunks + ' 段字幕 · ' + result.stats.llm_calls +
        ' 次 AI 调用 · ' + result.outline.length + ' 章 / 导图 ' +
        (result.stats.map_depth || '?') + ' 层 · ' + result.stats.provider + tail;
    }
    state.tab = 'note';
    renderTab();
  }

  function poll() {
    clearTimeout(state.timer);
    function tick() {
      fetch(API + '/summary/' + state.taskId).then(function (res) { return res.json(); }).then(function (b) {
        if (!b || !b.success) { throw new Error('查询任务失败'); }
        var t = b.data;
        setProgress(t.percent, t.message);
        if (t.partial && !state.result) { renderDraft(t.partial); }
        if (t.status === 'done') { showResult(t.result); return; }
        if (t.status === 'error') { showError(t.message || '总结失败'); return; }
        state.timer = setTimeout(tick, 1000);
      }).catch(function (e) { showError(e.message || '网络异常'); });
    }
    tick();
  }

  function submit() {
    setProgress(2, '正在提交 AI 总结任务…');
    var sub = $('#aiSub'); if (sub) { sub.textContent = '正在提交…'; }
    fetch(API + '/summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: state.url,
        cookies: localStorage.getItem('vg_cookie') || '',
        style: 'detailed',
        force: !!state.force
      })
    }).then(function (res) {
      return res.json().then(function (b) {
        if (!res.ok) { throw new Error(b.detail || ('请求失败（' + res.status + '）')); }
        return b;
      });
    }).then(function (b) {
      var d = b.data || {};
      state.taskId = d.id;
      state.reused = !!d.reused;
      // 后端幂等：已有结果直接秒开，0 次模型调用
      if (d.status === 'done' && d.result) {
        showResult(d.result, { cached: !!d.reused });
        return;
      }
      if (d.reused) setProgress(d.percent || 10, '已接回上次未完成的生成…');
      if (d.partial && !state.result) renderDraft(d.partial);
      openStream(0);
    }).catch(function (e) { showError(e.message || '提交失败'); });
  }

  function close() {
    clearTimeout(state.timer);
    state.timer = null;
    if (state.controller) { try { state.controller.abort(); } catch (e) { /* 已结束 */ } }
    state.controller = null;
    var m = document.getElementById('aiNoteMask');
    if (m) { m.remove(); }
  }

  function regen() {
    var url = state.url;
    delete noteCache[localKey(url)];
    if (state.controller) { try { state.controller.abort(); } catch (e) { /* 已结束 */ } }
    clearTimeout(state.timer);
    state = freshState(url);
    state.force = true;               // 让后端跳过索引，强制新建任务
    var prog = $('#aiProgress'); if (prog) { prog.hidden = false; }
    var tb = $('#aiToolbar'); if (tb) { tb.hidden = true; }
    var sub = $('#aiSub'); if (sub) { sub.textContent = '正在重新生成…'; }
    var body = $('#aiBody');
    if (body) { body.innerHTML = liveBar() + '<div class="ai-loading">正在重新生成（会重新调用模型）…</div>'; }
    submit();
  }

  function openPanel(url) {
    if (document.getElementById('aiNoteMask')) return;
    state = freshState(url);
    if (!document.getElementById('aiPrintHost')) {
      var ph = document.createElement('div');
      ph.id = 'aiPrintHost';
      document.body.appendChild(ph);
    }
    var mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.id = 'aiNoteMask';
    mask.innerHTML =
      '<div class="modal ai-modal" role="dialog" aria-modal="true">' +
        '<div class="ai-head"><div><h3>AI 视频总结</h3><p id="aiSub">正在提交任务…</p></div>' +
        '<button class="ai-close" id="aiClose" aria-label="关闭">×</button></div>' +
        '<div class="ai-progress" id="aiProgress"><div class="bar"><i id="aiBar"></i></div><span id="aiStage">准备中…</span></div>' +
        '<div class="ai-toolbar" id="aiToolbar" hidden>' +
          '<div class="ai-tabs"><button class="ai-tab is-on" data-tab="note">笔记</button>' +
          '<button class="ai-tab" data-tab="map">思维导图</button></div>' +
          '<span class="grow"></span>' +
          '<button class="btn btn-sm" data-act="copy">复制</button>' +
          '<button class="btn btn-sm" data-act="md">.md</button>' +
          '<button class="btn btn-sm" data-act="word">Word</button>' +
          '<button class="btn btn-sm" data-act="pdf">PDF</button>' +
          '<button class="btn btn-sm" data-act="svg">导图 SVG</button>' +
          '<button class="btn btn-sm ai-regen" data-act="regen" title="忽略缓存，重新调用模型生成">重新生成</button>' +
        '</div>' +
        '<div class="ai-body" id="aiBody">' + liveBar() +
          '<div class="ai-loading">正在连接实时生成通道…</div></div>' +
      '</div>';
    document.body.appendChild(mask);

    $('#aiClose', mask).onclick = close;
    mask.onclick = function (e) { if (e.target === mask) { close(); } };
    $('#aiToolbar', mask).addEventListener('click', function (e) {
      var tab = e.target.closest && e.target.closest('.ai-tab');
      if (tab) { state.tab = tab.getAttribute('data-tab'); renderTab(); return; }
      var act = e.target.closest && e.target.closest('[data-act]');
      if (act) {
        if (act.getAttribute('data-act') === 'regen') { regen(); return; }
        exportBy(act.getAttribute('data-act'));
      }
    });
    var cached = noteCache[localKey(url)];
    if (cached && cached.result) {
      state.taskId = cached.taskId || '';
      showResult(cached.result, { cached: true, at: cached.at });
    } else {
      submit();   // 只有 taskId 时会由后端幂等接回同一个任务
    }
  }

  // 事件委托：解析结果卡每次重绘都会重建 DOM，绑在 document 上才不会失效
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('#aiNoteBtn');
    if (!btn) return;
    var url = btn.getAttribute('data-url') || '';
    if (!url) { toast('请先解析一个视频', 'error'); return; }
    openPanel(url);
  });

  // 纯函数暴露给自测（线上逻辑不依赖）
  window.__vgAiNote = {
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
    regen: regen,
    getCache: function () { return noteCache; },
    getState: function () { return state; }
  };
})();
