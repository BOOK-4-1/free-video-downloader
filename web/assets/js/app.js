/* ==========================================================================
   秒下 VideoGrab —— 前端主逻辑（原生 JS，零构建，无需打包）
   流程：粘贴链接 → /api/parse → 选择清晰度 → /api/download → 轮询进度 → 下载文件
   ========================================================================== */
(function () {
  'use strict';

  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));

  const els = {
    form: $('#searchForm'),
    input: $('#urlInput'),
    parseBtn: $('#parseBtn'),
    clearBtn: $('#clearBtn'),
    pasteBtn: $('#pasteBtn'),
    root: $('#resultRoot'),
    toast: $('#toastRoot'),
  };

  let currentInfo = null;
  let selectedFormat = null;
  const polling = new Map(); // taskId -> timer

  /* ---------------- 工具 ---------------- */
  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...(opts || {}),
    });
    let body = null;
    try {
      body = await res.json();
    } catch (e) {
      body = null;
    }
    if (!res.ok) {
      const msg = (body && (body.detail || body.message)) || `请求失败（${res.status}）`;
      throw new Error(typeof msg === 'string' ? msg : '请求失败');
    }
    return body;
  }

  function toast(msg, type) {
    const div = document.createElement('div');
    div.className = 'toast ' + (type || '');
    const icon =
      type === 'error'
        ? '<svg class="ti" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>'
        : '<svg class="ti" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="m5 13 4 4L19 7"/></svg>';
    div.innerHTML = icon + '<span></span>';
    div.querySelector('span').textContent = msg;
    els.toast.appendChild(div);
    setTimeout(() => {
      div.style.transition = 'opacity .3s, transform .3s';
      div.style.opacity = '0';
      div.style.transform = 'translateY(-6px)';
      setTimeout(() => div.remove(), 320);
    }, type === 'error' ? 4200 : 2600);
  }

  const esc = (s) =>
    String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function thumbUrl(u) {
    if (!u) return '';
    return '/api/thumb?url=' + encodeURIComponent(u);
  }

  function fmtNum(n) {
    if (!n) return '0';
    if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
    return String(n);
  }

  /* ---------------- Cookie（本地保存，请求时带上） ---------------- */
  const CK = 'vg_cookie';
  const getCookie = () => localStorage.getItem(CK) || '';
  const setCookieVal = (v) => (v ? localStorage.setItem(CK, v) : localStorage.removeItem(CK));

  // 「导入 Cookie」弹窗统一由 header-ui.js 提供（含获取指引 + 验证 + 状态显示），
  // 这里只保留读写 localStorage 的小工具，避免两套实现互相覆盖。
  let suppressCookieModalUntil = 0;
  window.__vgLastUrl = () => (els.input ? els.input.value.trim() : '');
  window.__vgAfterCookieSaved = (status) => {
    if (window.__vgSyncCookieState) window.__vgSyncCookieState();
    const s = status || { count: 0, hasLogin: false };
    // 保存后会自动重解析一次；若失败，15 秒内不要再自动弹出 Cookie 弹窗，
    // 避免「保存 → 重解析失败 → 又弹窗」的连环弹窗
    suppressCookieModalUntil = Date.now() + 15000;
    if (!s.count) {
      toast('已保存到本机，但格式没识别出来，这份内容不会生效；点右上角「导入 Cookie」对照示例修改', 'error');
      return; // 格式都不对，重解析没有意义
    }
    toast(
      s.hasLogin
        ? 'Cookie 已保存到本机（' + s.count + ' 项），页眉按钮已变为「Cookie 已导入」'
        : '已保存到本机（' + s.count + ' 项），页眉按钮已变化；但缺登录态字段，可能仍解析失败',
      s.hasLogin ? 'ok' : 'error'
    );
    const u = els.input ? els.input.value.trim() : '';
    if (u) setTimeout(function () { doParse(u, { quiet: true }); }, 120);
  };

  function openCookieModal() {
    if (window.__vgOpenCookie) return window.__vgOpenCookie(els.input ? els.input.value.trim() : '');
    toast('页面脚本未就绪，请刷新后重试', 'error');
  }

  /* ---------------- 解析 ---------------- */
  function syncInputState() {
    els.form.classList.toggle('has-value', !!els.input.value.trim());
  }

  async function doParse(url, opts) {
    const quiet = !!(opts && opts.quiet);
    const raw = (url != null ? url : els.input.value).trim();
    if (!raw) {
      toast('先粘贴一个视频链接', 'error');
      els.input.focus();
      return;
    }
    els.parseBtn.disabled = true;
    els.parseBtn.textContent = '解析中';
    renderSkeleton();
    try {
      const res = await api('/api/parse', {
        method: 'POST',
        body: JSON.stringify({ url: raw, cookies: getCookie() }),
      });
      currentInfo = res.data;
      // 换链接解析必须重置档位：否则上一次单视频的 format_id（比如抖音的 dy-1080p）
      // 会漏到合集批量下载里，被服务端当成 B 站的档位 id 用
      selectedFormat = null;
      if (currentInfo.kind === 'playlist') {
        renderPlaylist(currentInfo);
      } else {
        selectedFormat =
          (currentInfo.formats.find((f) => f.recommended) || currentInfo.formats[0] || {}).format_id || 'best';
        renderResult(currentInfo);
      }
      els.root.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
      if (quiet) {
        // 保存 Cookie 后的自动重解析：保存提示已经发过一条 toast，
        // 这里只在结果区放一条静态说明，不再叠加第二条 toast、也不再弹窗
        els.root.hidden = false;
        els.root.innerHTML =
          '<div class="notice"><div><b>Cookie 已保存。</b>这次自动重新解析没成功：' +
          esc(e.message) + '。可以手动点「解析」重试，或换一条链接。</div></div>';
      } else {
        els.root.hidden = true;
        toast(e.message, 'error');
        // 刚保存过 Cookie 时不要再自动弹一次导入弹窗（否则用户会看到「弹两次」）
        if (/cookie/i.test(e.message) && Date.now() > suppressCookieModalUntil) {
          setTimeout(openCookieModal, 600);
        }
      }
    } finally {
      els.parseBtn.disabled = false;
      els.parseBtn.textContent = '解析';
    }
  }

  function renderSkeleton() {
    els.root.hidden = false;
    els.root.innerHTML = `
      <div class="card result-card">
        <div class="media">
          <div class="skeleton sk-figure" style="width:260px;border-radius:12px"></div>
          <div class="media-body">
            <div class="skeleton sk-line" style="width:80%;height:16px"></div>
            <div class="skeleton sk-line" style="width:50%"></div>
            <div class="skeleton sk-line" style="width:65%"></div>
          </div>
        </div>
        <div class="format-grid" style="margin-top:18px">
          ${'<div class="skeleton" style="height:52px"></div>'.repeat(4)}
        </div>
      </div>`;
  }

  function formatsHtml(formats, sel) {
    if (!formats.length) return '<div class="notice">这个视频没有返回可选清晰度，将按最佳可用质量下载。</div>';
    return formats
      .map((f) => {
        const isSel = f.format_id === sel;
        const audio = f.level === 'audio';
        return `<button type="button" class="format-item ${isSel ? 'selected' : ''}" data-fid="${esc(f.format_id)}">
          ${f.recommended ? '<span class="fr">推荐</span>' : ''}
          <span class="fl">${esc(f.label)}${f.fps && f.fps > 30 ? '<span class="faudio">' + Math.round(f.fps) + 'fps</span>' : ''}</span>
          <span class="fs">${esc(f.ext.toUpperCase())} · ${esc(f.filesize_text)}${f.need_merge ? ' · 视频+音频合并' : ''}</span>
        </button>`;
      })
      .join('');
  }

  // 编码提示：告诉用户「这一档实际会给什么编码」（切换清晰度时会跟着刷新）
  function renderCodecHint(formats, formatId) {
    const box = document.getElementById('codecHint');
    if (!box) return;
    const f = (formats || []).find((x) => x.format_id === formatId) || {};
    if (f.level === 'audio') {
      box.innerHTML = `<b>输出：仅音频</b>（M4A / AAC，可直接导入剪辑软件或音乐播放器）`;
      return;
    }
    const label = f.codec_label || '';
    if (!label) { box.textContent = ''; return; }
    box.innerHTML = f.h264
      ? `<b>本档可选编码：${esc(label)}</b>（兼容性最好，Windows / 手机 / 电视都能直接播）`
      : `<b>本档可选编码：${esc(label)}</b>（该站这一档没提供 H.264，已自动改用 ${esc(label)}；PotPlayer、VLC、现代浏览器都能播）`;
  }
  window.__vgCodecHint = renderCodecHint;

  function renderResult(d) {
    els.root.hidden = false;
    const formats = d.formats || [];
    els.root.innerHTML = `
      <div class="card result-card">
        <div class="media">
          <figure class="media-figure" id="previewBtn" title="预览">
            ${d.thumbnail ? `<img src="${thumbUrl(d.thumbnail)}" alt="${esc(d.title)}" loading="lazy" />` : '<div style="width:100%;height:100%"></div>'}
            <span class="media-duration">${esc(d.duration_text || '')}</span>
            <span class="media-play"><span>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="#1777ff"><path d="M8 5v14l11-7z"/></svg>
            </span></span>
          </figure>
          <div class="media-body">
            <h2 class="media-title">${esc(d.title)}</h2>
            <div class="media-meta">
              <span class="tag">${esc(d.platform_name || '视频')}</span>
              ${d.uploader ? `<span>${esc(d.uploader)}</span>` : ''}
              ${d.duration_text ? `<span class="sep">·</span><span>${esc(d.duration_text)}</span>` : ''}
              ${d.view_count ? `<span class="sep">·</span><span>${fmtNum(d.view_count)} 播放</span>` : ''}
              ${d.upload_date ? `<span class="sep">·</span><span>${esc(d.upload_date)}</span>` : ''}
              <span class="tag tag-plain">${esc(d.extractor || 'yt-dlp')}</span>
            </div>
            ${d.description ? `<p class="media-desc">${esc(d.description)}</p>` : ''}
            <div class="media-actions">
              <button class="btn btn-sm" id="copyTitleBtn">复制标题</button>
              <button class="btn btn-sm" id="aiNoteBtn" data-url="${esc(d.url)}">AI 总结</button>
              <a class="btn btn-sm" href="${esc(d.url)}" target="_blank" rel="noopener">原页面</a>
            </div>
          </div>
        </div>

        <div class="formats">
          <div class="formats-head">
            <h3>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1777ff" stroke-width="2.2" stroke-linecap="round"><path d="M4 6h16M4 12h10M4 18h7"/></svg>
              选择清晰度<span>共 ${formats.length} 个可用档位</span>
            </h3>
            <span id="selLabel"></span>
          </div>
          <div class="format-grid" id="formatGrid">${formatsHtml(formats, selectedFormat)}</div>
        </div>

        ${
          (d.parts || []).length
            ? `<div class="parts-bar">
                 <span class="tag">多 P 视频</span>
                 <span>这条视频共有 <b style="color:var(--primary)">${d.parts.length}</b> 个分 P，可一键批量下载</span>
                 <span class="grow" style="flex:1"></span>
                 <button class="btn btn-sm" id="partsBtn">批量下载全部分 P</button>
               </div>`
            : ''
        }

        <div class="download-bar">
          <div class="hint">默认优先输出 <b>H.264 (MP4)</b>，通用播放器都能直接放；平台只提供 AV1/HEVC 时会自动降级。<span id="codecHint"></span></div>
          <button class="btn btn-primary btn-lg" id="downloadBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7.5 11 4.5 4.5L16.5 11"/><path d="M4 20h16"/></svg>
            下载所选清晰度
          </button>
        </div>

        <div id="taskRoot"></div>
      </div>`;

    renderCodecHint(formats, selectedFormat);

    $('#formatGrid').addEventListener('click', (e) => {
      const btn = e.target.closest('.format-item');
      if (!btn) return;
      selectedFormat = btn.dataset.fid;
      $$('.format-item', els.root).forEach((b) => b.classList.toggle('selected', b.dataset.fid === selectedFormat));
      updateSelLabel();
      renderCodecHint(formats, selectedFormat);
    });
    $('#downloadBtn').onclick = startDownload;
    if ($('#partsBtn') && (d.parts || []).length) {
      $('#partsBtn').onclick = () => {
        const fake = { title: d.title, items: d.parts };
        startBatch(fake, new Set(d.parts.map((p) => p.index)));
      };
    }
    $('#previewBtn').onclick = () => {
      if (currentInfo && currentInfo.thumbnail) window.open(thumbUrl(currentInfo.thumbnail), '_blank');
      else window.open(currentInfo.url, '_blank');
    };
    $('#copyTitleBtn').onclick = async () => {
      try {
        await navigator.clipboard.writeText(d.title);
        toast('标题已复制', 'ok');
      } catch (err) {
        toast('复制失败，请手动选择', 'error');
      }
    };
    updateSelLabel();
  }

  function updateSelLabel() {
    const f = (currentInfo.formats || []).find((x) => x.format_id === selectedFormat);
    const el = $('#selLabel');
    if (!el) return;
    el.textContent = f ? `已选 ${f.label} · ${f.filesize_text}` : '';
  }

  function renderPlaylist(d) {
    els.root.hidden = false;
    const picked = new Set(d.items.map((it) => it.index)); // 默认全选
    els.root.innerHTML = `
      <div class="card result-card">
        <div class="batch-head">
          <h3>合集：${esc(d.title)}<span class="num">（共 ${d.count} 个视频）</span></h3>
          <span class="grow"></span>
          ${d.uploader ? `<span class="batch-sum">${esc(d.uploader)}</span>` : ''}
          <span class="tag">${esc(d.platform_name || '合集')}</span>
        </div>
        ${
          d.truncated
            ? `<div class="notice" style="margin-top:12px"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>
                <span>该合集共 ${d.count} 个视频，为避免一次拉取过多，这里只列出前 ${d.items.length} 个。</span></div>`
            : ''
        }
        <div class="playlist" id="plList">
          ${d.items
            .map(
              (it) => `<div class="pl-item checked" data-idx="${it.index}">
                <span class="ck"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4 4L19 7"/></svg></span>
                <span class="idx">${it.index}</span>
                <span class="pl-title" title="${esc(it.title)}">${esc(it.title)}</span>
                ${it.duration ? `<span class="pl-dur">${esc(it.duration_text)}</span>` : ''}
                <button class="btn btn-sm" data-single="${esc(it.url)}">单独下载</button>
              </div>`
            )
            .join('')}
        </div>
        <div class="pl-tools">
          <button class="lnk" id="plAll">全选</button>
          <button class="lnk" id="plNone">全不选</button>
          <button class="lnk" id="plFirst5">只选前 5 个</button>
          <span class="grow"></span>
          <span class="batch-sum" id="plCount"></span>
        </div>
        <div class="download-bar" style="margin-top:14px">
          <div class="hint">批量下载按「最佳清晰度」逐条进行，服务端排队执行；全部完成后可一键打包 zip。<b>请勿用于侵权传播</b>。</div>
          <button class="btn btn-primary btn-lg" id="batchBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7.5 11 4.5 4.5L16.5 11"/><path d="M4 20h16"/></svg>
            批量下载所选
          </button>
        </div>
        <div id="groupRoot"></div>
      </div>`;

    const list = $('#plList');
    const countEl = $('#plCount');
    const syncCount = () => {
      countEl.innerHTML = `已选 <b>${picked.size}</b> / ${d.items.length} 个`;
      $('#batchBtn').disabled = picked.size === 0;
    };
    const setAll = (on) => {
      picked.clear();
      $$('.pl-item', list).forEach((el) => {
        el.classList.toggle('checked', on);
        if (on) picked.add(Number(el.dataset.idx));
      });
      syncCount();
    };

    list.addEventListener('click', (e) => {
      const single = e.target.closest('[data-single]');
      if (single) {
        e.stopPropagation();
        els.input.value = single.dataset.single;
        syncInputState();
        doParse();
        return;
      }
      const row = e.target.closest('.pl-item');
      if (!row) return;
      const idx = Number(row.dataset.idx);
      if (picked.has(idx)) picked.delete(idx);
      else picked.add(idx);
      row.classList.toggle('checked', picked.has(idx));
      syncCount();
    });

    $('#plAll').onclick = () => setAll(true);
    $('#plNone').onclick = () => setAll(false);
    $('#plFirst5').onclick = () => {
      setAll(false);
      $$('.pl-item', list).slice(0, 5).forEach((el) => {
        el.classList.add('checked');
        picked.add(Number(el.dataset.idx));
      });
      syncCount();
    };
    $('#batchBtn').onclick = () => startBatch(d, picked);
    syncCount();

    // 多 P 视频：在单条结果里也能一键批量
  }

  /* ---------------- 批量下载 ---------------- */
  const groups = new Map(); // groupId -> { total, done, failed, title, finished }

  async function startBatch(d, picked) {
    const items = d.items.filter((it) => picked.has(it.index) && it.url).map((it) => ({ url: it.url, title: it.title }));
    if (!items.length) return toast('请先勾选要下载的视频', 'error');
    // 合集页的按钮是 #batchBtn，单视频页「批量下载全部分 P」是 #partsBtn：
    // 两个入口共用本函数，必须做空值保护，否则 parts 入口会直接抛 TypeError。
    const btn = $('#batchBtn') || $('#partsBtn');
    const label = btn ? btn.textContent : '';
    if (btn) {
      btn.disabled = true;
      btn.textContent = '提交中…';
    }
    try {
      const res = await api('/api/download/batch', {
        method: 'POST',
        body: JSON.stringify({
          items,
          format_id: selectedFormat || 'best',
          cookies: getCookie(),
          group_title: d.title || '合集下载',
        }),
      });
      const g = res.data;
      renderGroup(g.group_id, g.group_title, g.tasks);
      toast(`已提交 ${g.tasks.length} 个下载任务`, 'ok');
      pollGroup(g.group_id);
      g.tasks.forEach((t) => poll(t.id));
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = label;
      }
    }
  }

  function renderGroup(groupId, title, taskList) {
    const root = $('#groupRoot');
    if (!root) return;
    root.innerHTML = `
      <div class="batch-head" style="margin-top:16px">
        <h3>${esc(title)} · 批量任务</h3>
        <span class="grow"></span>
        <span class="batch-sum" id="gsum-${groupId}">进行中…</span>
        <a class="btn btn-sm" id="gzip-${groupId}" style="display:none" href="/api/group/${groupId}/zip">打包下载 zip</a>
      </div>
      <div class="group-tasks" id="gtasks-${groupId}"></div>`;
    const listEl = $(`#gtasks-${groupId}`);
    taskList.forEach((t, i) => {
      const div = document.createElement('div');
      div.className = 'task';
      div.id = 'task-' + t.id;
      div.innerHTML = `
        <div class="task-top">
          <span class="task-title">${t.group_index}. ${esc(t.title || '视频 ' + (i + 1))}</span>
          <span class="task-status" data-role="status">排队中…</span>
        </div>
        <div class="bar"><i data-role="bar"></i></div>
        <div class="task-foot">
          <span data-role="detail">准备中</span>
          <span class="grow"></span>
          <span class="task-actions" data-role="actions"></span>
        </div>`;
      listEl.appendChild(div);
    });
  }

  function pollGroup(groupId) {
    const tick = async () => {
      let g;
      try {
        const res = await api('/api/tasks/group/' + groupId);
        g = res.data;
      } catch (e) {
        return; // 组还没起来或已清理
      }
      const sum = document.getElementById('gsum-' + groupId);
      if (sum) {
        sum.innerHTML = `已完成 <b>${g.done}</b> / ${g.total}${g.failed ? ` · 失败 ${g.failed}` : ''}${
          g.filesize_text ? ' · 共 ' + g.filesize_text : ''
        }`;
      }
      const zip = document.getElementById('gzip-' + groupId);
      if (zip && g.done > 0) zip.style.display = '';
      if (g.finished) {
        stopGroupPoll(groupId);
        toast(`批量下载完成：成功 ${g.done} 个${g.failed ? `，失败 ${g.failed} 个` : ''}`, g.failed ? 'error' : 'ok');
        if (zip) zip.style.display = g.done > 0 ? '' : 'none';
        return;
      }
      groupTimers.set(groupId, setTimeout(tick, 1200));
    };
    tick();
  }
  const groupTimers = new Map();
  function stopGroupPoll(groupId) {
    const t = groupTimers.get(groupId);
    if (t) clearTimeout(t);
    groupTimers.delete(groupId);
  }

  /* ---------------- 下载 ---------------- */
  async function startDownload() {
    if (!currentInfo) return;
    const btn = $('#downloadBtn');
    btn.disabled = true;
    const taskRoot = $('#taskRoot');
    try {
      const res = await api('/api/download', {
        method: 'POST',
        body: JSON.stringify({
          url: currentInfo.url,
          format_id: selectedFormat || 'best',
          cookies: getCookie(),
          title: currentInfo.title,
          thumbnail: currentInfo.thumbnail,
          info: {
            title: currentInfo.title,
            thumbnail: currentInfo.thumbnail,
            platform_name: currentInfo.platform_name,
            duration_text: currentInfo.duration_text,
          },
        }),
      });
      addTaskCard(res.data);
      poll(res.data.id);
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  function addTaskCard(task) {
    const root = $('#taskRoot');
    const div = document.createElement('div');
    div.className = 'task';
    div.id = 'task-' + task.id;
    div.innerHTML = `
      <div class="task-top">
        <span class="task-title">${esc(task.title || '下载任务')}</span>
        <span class="task-status" data-role="status">排队中…</span>
      </div>
      <div class="bar"><i data-role="bar"></i></div>
      <div class="task-foot">
        <span data-role="detail">准备中</span>
        <span class="grow"></span>
        <span class="task-actions" data-role="actions"></span>
      </div>`;
    root.prepend(div);
    return div;
  }

  function poll(taskId) {
    if (polling.has(taskId)) return;
    const tick = async () => {
      try {
        const res = await api('/api/tasks/' + taskId);
        updateTask(res.data);
        if (['done', 'error'].includes(res.data.status)) {
          stopPoll(taskId);
          const btn = $('#downloadBtn');
          if (btn) btn.disabled = false;
        }
      } catch (e) {
        stopPoll(taskId);
      }
    };
    polling.set(taskId, setInterval(tick, 900));
    tick();
  }

  function stopPoll(taskId) {
    const t = polling.get(taskId);
    if (t) clearInterval(t);
    polling.delete(taskId);
  }

  function updateTask(t) {
    const card = document.getElementById('task-' + t.id);
    if (!card) return;
    const status = $('[data-role=status]', card);
    const bar = $('[data-role=bar]', card);
    const detail = $('[data-role=detail]', card);
    const actions = $('[data-role=actions]', card);

    if (t.status === 'done') {
      status.textContent = '已完成';
      status.className = 'task-status done';
      bar.style.width = '100%';
      bar.parentElement.classList.add('done');
      detail.textContent = `${t.total} · ${Math.round(t.expires_in / 60)} 分钟内有效`;
      if (!actions.dataset.filled) {
        actions.dataset.filled = '1';
        actions.innerHTML = `
          <a class="btn btn-primary btn-sm" href="${esc(t.file_url || '/api/file/' + t.id)}" download="${esc(
          (t.title || 'video') + (t.filename ? '.' + t.filename.split('.').pop() : '')
        )}">保存到本地</a>
          <button class="btn btn-sm" data-preview>预览</button>`;
        const pv = $('[data-preview]', actions);
        if (pv) {
          pv.onclick = () => {
            const mask = document.createElement('div');
            mask.className = 'modal-mask';
            mask.innerHTML = `<div class="modal" style="width:min(820px,100%);padding:14px">
                <video src="/api/file/${t.id}?inline=true" controls autoplay style="width:100%;border-radius:10px;background:#000"></video>
                <div class="modal-foot"><button class="btn btn-sm" id="pvClose">关闭</button></div>
              </div>`;
            document.body.appendChild(mask);
            $('#pvClose', mask).onclick = () => mask.remove();
            mask.onclick = (e) => { if (e.target === mask) mask.remove(); };
          };
        }
      }
      if (!t.group_id) toast('下载完成，点击「保存到本地」', 'ok');
    } else if (t.status === 'error') {
      status.textContent = '失败';
      status.className = 'task-status error';
      bar.style.width = '0';
      detail.textContent = t.error || '下载失败';
      actions.innerHTML = '<button class="btn btn-sm" data-retry>重试</button>';
      $('[data-retry]', actions).onclick = () => {
        card.remove();
        startDownload();
      };
      toast(t.error || '下载失败', 'error');
    } else {
      const merging = t.status === 'merging';
      status.textContent = merging ? '合并音视频…' : t.status === 'queued' ? '排队中…' : t.percent + '%';
      bar.style.width = (merging ? 99.9 : t.percent) + '%';
      detail.textContent = merging
        ? '正在用 ffmpeg 合并高清视频与音频，请稍候'
        : [t.speed, t.eta ? '剩余 ' + t.eta : '', t.downloaded + (t.total && t.total !== '未知大小' ? ' / ' + t.total : '')]
            .filter(Boolean)
            .join(' · ') || '连接中…';
    }
  }

  /* ---------------- 统计 ---------------- */
  async function loadStats() {
    try {
      const res = await api('/api/stats');
      const d = res.data;
      $('#statSites').innerHTML = d.extractors + '<span class="unit">个</span>';
      $('#statExtractors').textContent = d.extractors;
      $('#statTasks').innerHTML = d.tasks_done + '<span class="unit">次</span>';
      $('#statTtl').innerHTML = d.file_ttl_min + '<span class="unit">分钟</span>';
    } catch (e) {
      /* 忽略：统计失败不影响主流程 */
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    doParse();
  });
  els.input.addEventListener('input', syncInputState);
  els.clearBtn.onclick = () => {
    els.input.value = '';
    syncInputState();
    els.input.focus();
    els.root.hidden = true;
  };
  els.pasteBtn.onclick = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return toast('剪贴板是空的', 'error');
      els.input.value = text.trim();
      syncInputState();
      doParse();
    } catch (e) {
      toast('浏览器不允许读取剪贴板，请手动粘贴', 'error');
      els.input.focus();
    }
  };
  // 「导入 Cookie」（页眉与页脚）统一由 header-ui.js 接管，两页共用同一套实现

  // 支持 ?url= 直接带入
  const q = new URLSearchParams(location.search).get('url');
  if (q) {
    els.input.value = q;
    syncInputState();
    doParse();
  }


  syncInputState();
  loadStats();
  setInterval(loadStats, 30000);
})();
