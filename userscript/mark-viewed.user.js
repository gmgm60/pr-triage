// ==UserScript==
// @name         PR Triage: Mark pre-cleared files as viewed
// @namespace    https://github.com/gmgm60/pr-triage
// @version      1.0.2
// @description  One-click "mark the PR Triage bot's pre-cleared files as Viewed" on GitHub PR Files changed pages
// @author       gmgm60
// @license      MIT
// @match        https://github.com/*/pull/*/files*
// @match        https://github.com/*/pull/*/changes*
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
    // Primary: scrape the rendered <details> list from the triage comment
    // (GitHub's markdown sanitizer keeps <details>/<li>/<code> in the DOM).
    const doc = new DOMParser().parseFromString(html, 'text/html');
    for (const details of doc.querySelectorAll('details')) {
      const summary = details.querySelector('summary');
      if (summary && /pre-cleared files/.test(summary.textContent)) {
        const paths = [...details.querySelectorAll('li code')]
          .map(code => code.textContent.trim())
          .filter(Boolean);
        if (paths.length) return paths;
      }
    }
    // Fallback: hidden HTML comment marker (present in some embeds)
    const m = html.match(/<!--\s*pr-triage-safe:\s*(\[[\s\S]*?\])\s*-->/);
    if (m) {
      try {
        const list = JSON.parse(m[1]);
        if (Array.isArray(list) && list.length) return list;
      } catch (e) { /* ignore */ }
    }
    return null;
  }

  function pathFromBlobLink(container) {
    // Works on both the classic and React file views: the file header links to
    // the blob — /owner/repo/blob/<sha>/<path...>
    for (const a of container.querySelectorAll('a[href*="/blob/"]')) {
      const m = a.getAttribute('href').match(/^\/[^/]+\/[^/]+\/blob\/[^/]+\/(.+)$/);
      if (m) {
        try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
      }
    }
    return null;
  }

  function anchorOf(container) {
    // GitHub identifies each file by the anchor #diff-<sha256(path)> — on the
    // React view the header links to it, on the classic view the header
    // carries data-anchor. Verified: sha256(path) === the anchor hash.
    const header = container.querySelector(
      'div[class*="diff-file-header"], .file-header');
    if (header) {
      const a = header.querySelector('a[href^="#diff-"]');
      if (a) return a.getAttribute('href').slice(1);
      const da = header.getAttribute('data-anchor');
      if (da && da.startsWith('diff-')) return da;
    }
    const a = container.querySelector('a[href^="#diff-"]');
    if (a) return a.getAttribute('href').slice(1);
    const da = container.getAttribute && container.getAttribute('data-anchor');
    if (da && da.startsWith('diff-')) return da;
    if (container.id && container.id.startsWith('diff-')) return container.id;
    return null;
  }

  async function sha256Hex(text) {
    const buf = await crypto.subtle.digest(
      'SHA-256', new TextEncoder().encode(text));
    return [...new Uint8Array(buf)]
      .map(b => b.toString(16).padStart(2, '0')).join('');
  }

  async function buildAnchorMap(paths) {
    // Map: 'diff-<sha256(path)>' -> path
    const map = new Map();
    await Promise.all(paths.map(async p => {
      map.set('diff-' + await sha256Hex(p), p);
    }));
    return map;
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
      if (!path) path = pathFromBlobLink(container);
      entries.push({ container, toggle, path, anchor: anchorOf(container) });
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

  function markEntries(entries, safePaths, anchorMap) {
    // Match by diff anchor (GitHub's own #diff-<sha256(path)> id) first —
    // works on both classic and React views, immune to rename rendering —
    // with plain path matching as fallback.
    const wantPaths = new Set(safePaths);
    const matched = new Set();
    let changed = 0, already = 0;
    for (const entry of entries) {
      let path = null;
      if (entry.anchor && anchorMap.has(entry.anchor)) {
        path = anchorMap.get(entry.anchor);
      } else if (entry.path && wantPaths.has(entry.path)) {
        path = entry.path;
      }
      if (!path) continue;
      matched.add(path);
      if (isViewed(entry)) { already++; continue; }
      entry.toggle.click();
      changed++;
    }
    const missing = safePaths.filter(p => !matched.has(p));
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
      const anchorMap = await buildAnchorMap(safe);
      const { changed, already, missing } = markEntries(entries, safe, anchorMap);
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
  // Also re-init when the SPA mutates into the files/changes view
  new MutationObserver(() => {
    if ((location.pathname.includes('/files') ||
         location.pathname.includes('/changes')) &&
        !document.getElementById('pr-triage-mark-btn')) {
      init();
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
