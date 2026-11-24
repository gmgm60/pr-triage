// ==UserScript==
// @name         PR Triage: Mark pre-cleared files as viewed
// @namespace    https://github.com/gmgm60/pr-triage
// @version      1.0.0
// @description  One-click "mark the PR Triage bot's pre-cleared files as Viewed" on GitHub PR Files changed pages
// @author       gmgm60
// @license      MIT
// @match        https://github.com/*/pull/*/files*
// @match        https://github.com/*/pull/*/commits/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/*
 * How it works:
 *   1. Reads the PR number from the URL.
 *   2. Fetches the PR conversation page (same-origin, your session) and extracts
 *      the safe-file list the PR Triage bot embedded in its report comment
 *      (<!-- pr-triage-safe: [...] -->).
 *   3. Finds each file's "Viewed" toggle on the Files changed page and clicks
 *      the ones that are not yet viewed. Clicking uses GitHub's own controls,
 *      so it saves exactly like a manual click. No tokens, no API access.
 *
 * Selector strategy adapted from the MIT-licensed Refined GitHub feature
 * "batch-mark-files-as-viewed" (refined-github/refined-github) — supports both
 * the current React UI (button[aria-label="Viewed"]) and the older
 * input.js-reviewed-checkbox markup.
 */

(() => {
  'use strict';

  const VIEWED_TOGGLE = [
    'button[aria-label="Viewed"]',
    'button[aria-label="Not Viewed"]',
    'input.js-reviewed-checkbox',
  ].join(', ');
  const FILE_CONTAINER = [
    '[class*="diffTargetable"]',
    '.js-file',
    '.file.js-file',
  ].join(', ');

  function prInfo() {
    const m = location.pathname.match(/^\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
    return m ? { owner: m[1], repo: m[2], number: m[3] } : null;
  }

  function toast(message, ms = 4000) {
    const el = document.createElement('div');
    el.textContent = message;
    Object.assign(el.style, {
      position: 'fixed', bottom: '24px', right: '24px', zIndex: 9999,
      background: '#24292f', color: '#fff', padding: '10px 16px',
      borderRadius: '6px', fontSize: '13px', fontFamily:
        '-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',
      boxShadow: '0 4px 12px rgba(0,0,0,.25)',
      transition: 'opacity .3s',
    });
    document.body.append(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, ms);
  }

  async function fetchSafeList(info) {
    const url = `/${info.owner}/${info.repo}/pull/${info.number}`;
    const resp = await fetch(url, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`fetch PR page: HTTP ${resp.status}`);
    const html = await resp.text();
    const m = html.match(/<!--\s*pr-triage-safe:\s*(\[[\s\S]*?\])\s*-->/);
    if (!m) return null;
    try {
      const list = JSON.parse(m[1]);
      return Array.isArray(list) ? list : null;
    } catch (e) {
      return null;
    }
  }

  function fileEntries() {
    const entries = [];
    for (const container of document.querySelectorAll(FILE_CONTAINER)) {
      if (!container.checkVisibility || container.checkVisibility() === false) continue;
      const toggle = container.querySelector(VIEWED_TOGGLE);
      if (!toggle) continue;
      let path = null;
      const header =
        container.querySelector('.file-header[data-path]') ||
        container.querySelector('[data-path]');
      if (header) path = header.getAttribute('data-path');
      if (!path) {
        const tagged = container.querySelector('[data-tagsearch-path]');
        if (tagged) path = tagged.getAttribute('data-tagsearch-path');
      }
      entries.push({ container, toggle, path });
    }
    return entries;
  }

  function isViewed(entry) {
    const t = entry.toggle;
    if (t instanceof HTMLInputElement) return t.checked;
    return t.getAttribute('aria-pressed') === 'true';
  }

  function markAll(entries, markViewed) {
    let changed = 0;
    for (const entry of entries) {
      if (isViewed(entry) !== markViewed) {
        entry.toggle.click();
        changed++;
      }
    }
    return changed;
  }

  function markPaths(entries, paths) {
    const want = new Set(paths);
    let changed = 0, already = 0, missing = [];
    for (const entry of entries) {
      if (!entry.path) continue;
      if (!want.has(entry.path)) continue;
      if (isViewed(entry)) { already++; continue; }
      entry.toggle.click();
      changed++;
      want.delete(entry.path);
    }
    for (const p of want) missing.push(p); // safe paths not rendered on this page
    return { changed, already, missing };
  }

  function buildButton() {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '✅ Mark pre-cleared as viewed';
    Object.assign(btn.style, {
      position: 'fixed', bottom: '24px', left: '24px', zIndex: 9999,
      background: '#2da44e', color: '#fff', border: '1px solid rgba(27,31,36,.15)',
      padding: '8px 14px', borderRadius: '6px', fontSize: '13px', fontWeight: 600,
      cursor: 'pointer', fontFamily:
        '-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',
      boxShadow: '0 4px 12px rgba(0,0,0,.18)',
    });
    btn.addEventListener('mouseenter', () => (btn.style.background = '#2c974b'));
    btn.addEventListener('mouseleave', () => (btn.style.background = '#2da44e'));
    return btn;
  }

  let busy = false;

  async function onMarkClick(info) {
    if (busy) return;
    busy = true;
    try {
      const entries = fileEntries();
      if (entries.length === 0) {
        toast('No files with Viewed controls found on this page.');
        return;
      }
      const safe = await fetchSafeList(info);
      if (!safe) {
        const ok = confirm(
          'No PR Triage report found on this PR.\n\n' +
          'Mark ALL files as viewed instead?');
        if (ok) {
          const changed = markAll(entries, true);
          toast(`Marked ${changed} file(s) as viewed (all).`);
        }
        return;
      }
      const { changed, already, missing } = markPaths(entries, safe);
      let msg = `Marked ${changed} pre-cleared file(s) as viewed` +
                (already ? ` · ${already} already viewed` : '');
      if (missing.length) {
        msg += ` · ${missing.length} not on this page`;
      }
      const remaining = entries.filter(e => !isViewed(e)).length;
      if (remaining) msg += ` · ${remaining} left for review`;
      toast(msg, 6000);
    } catch (e) {
      toast(`PR Triage error: ${e.message}`);
    } finally {
      busy = false;
    }
  }

  function init() {
    const info = prInfo();
    if (!info) return;
    if (document.getElementById('pr-triage-mark-btn')) return;
    const btn = buildButton();
    btn.id = 'pr-triage-mark-btn';
    btn.addEventListener('click', () => onMarkClick(info));
    document.body.append(btn);
    // Re-add after GitHub soft-navigation (turbo/pjax) replaces the body
    document.addEventListener('turbo:load', () => {
      if (prInfo() && !document.getElementById('pr-triage-mark-btn')) {
        document.body.append(btn);
      }
    });
  }

  init();
  // Also re-init when the SPA mutates into the files view
  new MutationObserver(() => {
    if (location.pathname.includes('/files') &&
        !document.getElementById('pr-triage-mark-btn')) {
      init();
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
