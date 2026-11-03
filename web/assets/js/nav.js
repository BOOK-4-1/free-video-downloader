/**
 * 全站共用的导航高亮（首页 + 卡密兑换页）
 *
 * 规则：
 *  - 首页：跟着滚动走，滚到哪个区块点亮哪一项；滚回顶部点亮「在线下载」
 *  - 卡密兑换页（/store）：静态点亮「卡密兑换」
 *  - 从别的页面点 /#pricing 这类跨页锚点进来时，定位到区块并点亮对应项
 *  - 点击导航项时立即点亮，不等滚动结束
 *
 * 依赖：导航项上有 data-nav="home|features|how|pricing|store|faq"
 * 注意：样式由 style.css 里的 .nav a.active 提供
 */
(function () {
  'use strict';

  var isHome = !/^\/store(\/|$)/.test(location.pathname);
  var SECTION_KEYS = ['features', 'how', 'pricing', 'faq'];

  function setActive(link) {
    var links = document.querySelectorAll('.nav a[data-nav]');
    for (var i = 0; i < links.length; i++) {
      links[i].classList.toggle('active', links[i] === link);
    }
  }

  function headerHeight() {
    var h = document.querySelector('.header');
    return (h ? h.offsetHeight : 56) + 24;
  }

  function boot() {
    var links = document.querySelectorAll('.nav a[data-nav]');
    if (!links.length) return;

    var byKey = {};
    for (var i = 0; i < links.length; i++) {
      byKey[links[i].getAttribute('data-nav')] = links[i];
    }

    // 非首页：静态点亮当前页面对应的项（卡密兑换）
    if (!isHome) {
      if (byKey.store) setActive(byKey.store);
      return;
    }

    // 首页：把参与滚动的区块收集起来
    var items = [];
    for (var k = 0; k < SECTION_KEYS.length; k++) {
      var key = SECTION_KEYS[k];
      var link = byKey[key];
      var target = document.getElementById(key);
      if (link && target) items.push({ key: key, link: link, target: target });
    }
    if (!items.length) return;

    var homeLink = byKey.home;

    // 跨页/带锚点进入：滚到区块并点亮（用时间窗而非布尔锁，避免永久屏蔽滚动）
    var jumpUntil = 0;
    function applyHash() {
      var hash = (location.hash || '').slice(1);
      if (!hash) return false;
      var hit = null;
      for (var j = 0; j < items.length; j++) if (items[j].key === hash) hit = items[j];
      if (!hit) return false;
      var top = hit.target.getBoundingClientRect().top + window.pageYOffset - headerHeight() + 8;
      window.scrollTo({ top: top, behavior: 'auto' });
      setActive(hit.link);
      jumpUntil = Date.now() + 150;
      return true;
    }
    applyHash();
    window.addEventListener('hashchange', applyHash);

    var ticking = false;
    function onScroll() {
      if (ticking || Date.now() < jumpUntil) return;
      ticking = true;
      requestAnimationFrame(function () {
        ticking = false;
        var line = headerHeight();

        // 已经滚到底部：点亮最后一项
        if (window.innerHeight + window.pageYOffset >= document.body.scrollHeight - 2) {
          setActive(items[items.length - 1].link);
          return;
        }

        var current = null;
        for (var j = 0; j < items.length; j++) {
          if (items[j].target.getBoundingClientRect().top <= line) current = items[j].link;
        }
        setActive(current || homeLink);
      });
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);

    // 点击立即高亮（浏览器随后会自己滚过去）
    for (var m = 0; m < items.length; m++) {
      (function (item) {
        item.link.addEventListener('click', function () {
          setActive(item.link);
          jumpUntil = Date.now() + 400; // 交给浏览器滚动，滚完再恢复监听
        });
      })(items[m]);
    }
    if (homeLink) homeLink.addEventListener('click', function () { setActive(homeLink); });

    onScroll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
