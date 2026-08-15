(() => {
  const ARABIC_CHAR = /[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/;
  const HIGHLIGHT_NAME = 'ata-arabic-focus';

  // 아랍어 단어는 한 텍스트 노드로 유지하고 *...*의 문자 범위만 기록한다.
  function render(source, className) {
    return String(source).replace(/[^\s<>]*\*[^*\s<>]+\*[^\s<>]*/g, token => {
      const plainToken = token.replace(/\*/g, '');
      if (!ARABIC_CHAR.test(plainToken)) {
        return token.replace(/\*([^*\n]+)\*/g, `<span class="${className}">$1</span>`);
      }

      let text = '';
      let cursor = 0;
      const ranges = [];
      token.replace(/\*([^*\n]+)\*/g, (full, content, offset) => {
        text += token.slice(cursor, offset);
        const start = text.length;
        text += content;
        ranges.push(`${start}:${text.length}`);
        cursor = offset + full.length;
        return full;
      });
      text += token.slice(cursor);
      return `<span class="ata-range-text" data-ata-ranges="${ranges.join(',')}">${text}</span>`;
    }).replace(/\*([^*\n]+)\*/g, `<span class="${className}">$1</span>`);
  }

  function refresh() {
    const ranges = [];
    document.querySelectorAll('[data-ata-ranges]').forEach(el => {
      const node = el.firstChild;
      if (!node || node.nodeType !== Node.TEXT_NODE) return;
      el.dataset.ataRanges.split(',').forEach(spec => {
        const [start, end] = spec.split(':').map(Number);
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > node.length || start >= end) return;
        const range = new Range();
        range.setStart(node, start);
        range.setEnd(node, end);
        ranges.push(range);
      });
    });
    CSS.highlights.set(HIGHLIGHT_NAME, new Highlight(...ranges));
  }

  function install() {
    if (!globalThis.CSS?.highlights || typeof Highlight === 'undefined') {
      document.documentElement.classList.add('ata-highlight-fallback');
      return;
    }
    let queued = false;
    const schedule = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; refresh(); });
    };
    new MutationObserver(schedule).observe(document.body, { childList:true, subtree:true });
    schedule();
  }

  globalThis.ATAInlineHighlights = { render };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, { once:true });
  else install();
})();
