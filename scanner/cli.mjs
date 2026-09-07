#!/usr/bin/env node
/**
 * Scanner CLI. Runs once per page, writes JSON, exits.
 *
 * Two modes, one binary (see design spec §3):
 *   scan     --url <url> --out <file>            full page scan + node index + perf
 *   validate --url <url> --selector <sel>        apply a patch, re-run axe on the
 *            --patch-file <file> --out <file>    subtree, report cleared/introduced
 *
 * Deliberately a subprocess and not a service: nothing to keep alive, and the
 * validate path reuses the exact same page-loading code as the scan path.
 */
import { chromium } from 'playwright';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const AXE_SOURCE = readFileSync(require.resolve('axe-core'), 'utf8');

// ---------------------------------------------------------------- args

function parseArgs(argv) {
  const mode = argv[2];
  const out = {};
  for (let i = 3; i < argv.length; i += 2) {
    if (!argv[i]?.startsWith('--')) continue;
    out[argv[i].slice(2)] = argv[i + 1];
  }
  return { mode, ...out };
}

function resolveTarget(url) {
  if (/^https?:\/\//i.test(url)) return url;
  return pathToFileURL(url).href; // local fixture path
}

// ---------------------------------------------------------------- in-page

/**
 * Builds the addressable node index. Runs inside the page.
 *
 * Only the computed-style properties the recipes actually consume are captured
 * (design spec §7.2) - grabbing all ~340 computed properties per node would make
 * the index an order of magnitude larger for no gain.
 */
const COLLECT_NODES = `(() => {
  const STYLE_PROPS = [
    'color','background-color','background-image','font-size','font-weight',
    'display','visibility','opacity','position'
  ];
  const LANDMARKS = new Set(['header','nav','main','footer','aside','form','section','article']);

  function cssPath(el) {
    // Stable, unique, human-readable enough to show a developer.
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      let seg = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) seg += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(seg);
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  function landmarkPath(el) {
    // Compressed ancestry: 'main > section[aria-label="News"] > figure'
    const chain = [];
    let node = el.parentElement;
    while (node && node !== document.body) {
      const tag = node.tagName.toLowerCase();
      const role = node.getAttribute('role');
      if (LANDMARKS.has(tag) || role) {
        const label = node.getAttribute('aria-label');
        let seg = role ? tag + '[role="' + role + '"]' : tag;
        if (label) seg += '[aria-label="' + label + '"]';
        chain.unshift(seg);
      }
      node = node.parentElement;
    }
    return chain.join(' > ');
  }

  function ownText(el) {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.replace(/\\s+/g, ' ').trim().slice(0, 300);
  }

  function implicitRole(el) {
    const tag = el.tagName.toLowerCase();
    const map = {
      a: el.hasAttribute('href') ? 'link' : null, button: 'button', nav: 'navigation',
      main: 'main', header: 'banner', footer: 'contentinfo', ul: 'list', ol: 'list',
      li: 'listitem', img: el.getAttribute('alt') === '' ? 'presentation' : 'img',
      h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading',
      form: 'form', figure: 'figure', section: el.hasAttribute('aria-label') ? 'region' : null,
      input: ({ email: 'textbox', text: 'textbox', checkbox: 'checkbox',
                submit: 'button', search: 'searchbox' })[el.getAttribute('type')] || 'textbox'
    };
    return map[tag] || null;
  }

  const all = Array.from(document.querySelectorAll('*'))
    .filter(el => !['SCRIPT','STYLE','META','LINK','BR'].includes(el.tagName));

  const nodes = all.map((el, i) => {
    const cs = getComputedStyle(el);
    const styles = {};
    for (const p of STYLE_PROPS) styles[p] = cs.getPropertyValue(p);
    const attrs = {};
    for (const a of el.attributes) attrs[a.name] = a.value.slice(0, 300);
    const rect = el.getBoundingClientRect();
    return {
      idx: i,
      selector: cssPath(el),
      tag: el.tagName.toLowerCase(),
      attrs,
      own_text: ownText(el),
      landmark_path: landmarkPath(el),
      explicit_role: el.getAttribute('role') || null,
      implicit_role: implicitRole(el),
      styles,
      rect: { x: Math.round(rect.x), y: Math.round(rect.y),
              w: Math.round(rect.width), h: Math.round(rect.height) },
      parent_selector: el.parentElement && el.parentElement !== document.documentElement
        ? cssPath(el.parentElement) : null,
      outer_html: el.outerHTML.slice(0, 1200)
    };
  });

  return { nodes, dom_node_count: document.querySelectorAll('*').length };
})()`;

/**
 * Performance signals for the LCP recipe (design spec §7.2).
 * LCP is a chain problem, so we capture the element AND everything that could
 * have delayed it - render-blocking head resources in document order.
 */
const COLLECT_PERF = `(() => {
  // Chromium does not expose LCP via getEntriesByType() on the default timeline,
  // so the buffered PerformanceObserver installed in addInitScript is the real
  // source; the timeline lookup stays only as a fallback for other engines.
  const lcpEntries = (window.__lcp && window.__lcp.length)
    ? window.__lcp
    : performance.getEntriesByType('largest-contentful-paint');
  const last = lcpEntries[lcpEntries.length - 1];
  const nav = performance.getEntriesByType('navigation')[0];
  const resources = performance.getEntriesByType('resource').map(r => ({
    url: r.name,
    type: r.initiatorType,
    start: Math.round(r.startTime),
    duration: Math.round(r.duration),
    transfer_size: r.transferSize || 0,
    decoded_size: r.decodedBodySize || 0
  }));

  // Render-blocking = stylesheet <link> in <head> with no non-blocking media,
  // plus classic <script src> without async/defer.
  const blocking = [];
  document.head.querySelectorAll('link[rel="stylesheet"], script[src]').forEach((el, order) => {
    if (el.tagName === 'LINK') {
      const media = el.getAttribute('media');
      const blocks = !media || media === 'all' || media === 'screen';
      blocking.push({ kind: 'stylesheet', href: el.getAttribute('href'),
                      media: media || '(none)', blocking: blocks, document_order: order });
    } else if (!el.hasAttribute('async') && !el.hasAttribute('defer')) {
      blocking.push({ kind: 'script', href: el.getAttribute('src'),
                      media: null, blocking: true, document_order: order });
    }
  });

  const hints = Array.from(document.head.querySelectorAll('link[rel="preload"],link[rel="preconnect"],link[rel="dns-prefetch"]'))
    .map(l => ({ rel: l.getAttribute('rel'), href: l.getAttribute('href'), as: l.getAttribute('as') }));

  let lcpSelector = null, lcpTag = null;
  if (last && last.element) {
    lcpTag = last.element.tagName.toLowerCase();
    lcpSelector = last.element.id ? '#' + last.element.id : null;
    if (!lcpSelector) {
      const parts = [];
      let n = last.element;
      while (n && n.nodeType === 1 && n !== document.documentElement) {
        let seg = n.tagName.toLowerCase();
        const p = n.parentElement;
        if (p) {
          const sibs = Array.from(p.children).filter(c => c.tagName === n.tagName);
          if (sibs.length > 1) seg += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
        }
        parts.unshift(seg);
        n = n.parentElement;
      }
      lcpSelector = parts.join(' > ');
    }
  }

  return {
    lcp: last ? {
      value_ms: Math.round(last.startTime),
      size: last.size,
      element_selector: lcpSelector,
      element_tag: lcpTag,
      url: last.url || null
    } : null,
    navigation: nav ? {
      dom_content_loaded: Math.round(nav.domContentLoadedEventEnd),
      load_event: Math.round(nav.loadEventEnd),
      response_end: Math.round(nav.responseEnd)
    } : null,
    render_blocking: blocking,
    resource_hints: hints,
    resources
  };
})()`;

// ---------------------------------------------------------------- page setup

async function openPage(browser, target, html) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  // LCP only reports for entries observed during load, so register before navigating.
  await page.addInitScript(() => {
    window.__lcp = [];
    try {
      new PerformanceObserver((l) => { window.__lcp.push(...l.getEntries()); })
        .observe({ type: 'largest-contentful-paint', buffered: true });
    } catch (e) { /* unsupported */ }
  });
  if (html) {
    await page.setContent(html, { waitUntil: 'load' });
  } else {
    await page.goto(target, { waitUntil: 'load', timeout: 30000 });
  }
  await page.waitForTimeout(400); // let LCP settle
  return page;
}

async function runAxe(page, contextExpr) {
  await page.addScriptTag({ content: AXE_SOURCE });
  return page.evaluate(async (ctx) => {
    const context = ctx ? document.querySelector(ctx) : document;
    if (!context) return { __error: 'context_not_found' };
    return await window.axe.run(context, { resultTypes: ['violations'] });
  }, contextExpr);
}

// ---------------------------------------------------------------- modes

async function doScan(args) {
  const target = resolveTarget(args.url);
  const browser = await chromium.launch();
  try {
    const page = await openPage(browser, target, args.html ? readFileSync(args.html, 'utf8') : null);

    const axeResult = await runAxe(page, null);
    const { nodes, dom_node_count } = await page.evaluate(COLLECT_NODES);

    // axe reports its own selector syntax, which will not match the cssPath used
    // by the node index. Resolve each violation target back to a cssPath inside
    // the page so the Python side can join issues to indexed nodes by key.
    const axeTargets = [];
    for (const v of axeResult.violations) {
      for (const n of v.nodes) {
        axeTargets.push(Array.isArray(n.target) ? n.target.join(' ') : String(n.target));
      }
    }
    const resolvedTargets = await page.evaluate((targets) => {
      const cssPath = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && node !== document.documentElement) {
          let seg = node.tagName.toLowerCase();
          const parent = node.parentElement;
          if (parent) {
            const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
            if (sibs.length > 1) seg += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
          }
          parts.unshift(seg);
          node = node.parentElement;
        }
        return parts.join(' > ');
      };
      const out = {};
      for (const t of targets) {
        try {
          const el = document.querySelector(t);
          out[t] = el ? cssPath(el) : null;
        } catch (e) { out[t] = null; }
      }
      return out;
    }, axeTargets);
    const perf = await page.evaluate(COLLECT_PERF);
    const html = await page.content();

    const violations = axeResult.violations.map(v => ({
      rule: v.id,
      impact: v.impact,
      help: v.help,
      description: v.description,
      help_url: v.helpUrl,
      tags: v.tags,
      // The deterministic anchor (design spec §5.1): axe already tells us the
      // success criterion, so criterion identity is a lookup, never a guess.
      sc_tags: v.tags.filter(t => /^wcag\d{3,4}$/.test(t)),
      nodes: v.nodes.map(n => {
        const target = Array.isArray(n.target) ? n.target.join(' ') : String(n.target);
        return {
          target,
          node_selector: resolvedTargets[target] ?? null, // join key into the node index
          html: n.html,
          failure_summary: n.failureSummary,
          impact: n.impact
        };
      })
    }));

    const payload = {
      scanned_at: new Date().toISOString(),
      url: args.url,
      resolved_url: target,
      content_hash: createHash('sha256').update(html).digest('hex').slice(0, 16),
      dom_node_count,
      indexed_node_count: nodes.length,
      violations,
      nodes,
      perf,
      axe_version: axeResult.testEngine?.version ?? null
    };
    writeFileSync(args.out, JSON.stringify(payload));
    console.error(`scan ok: ${violations.length} rules violated, ${nodes.length} nodes indexed`);
  } finally {
    await browser.close();
  }
}

/**
 * validate_fix (design spec §6.3).
 *
 * Runs against the real page, not a detached fragment: CSS context determines
 * contrast and the ancestor chain determines ARIA computation, so a fragment
 * parsed in isolation would give confidently wrong answers.
 *
 * SECURITY: the `outerHTML` assignment below deliberately executes model-authored
 * markup. That is the point of the tool - we are asking a real browser whether a
 * real patch really fixes a real violation, and no parser-only approximation gives
 * the same answer. It is acceptable here because the browser is headless, throwaway,
 * has no credentials or storage, and is closed immediately after. Before this shipped
 * to anything multi-tenant it would need a hardened sandbox (isolated origin, no
 * network, seccomp), because the patch text originates from an LLM.
 */
async function doValidate(args) {
  const target = resolveTarget(args.url);
  const patch = readFileSync(args['patch-file'], 'utf8');
  const browser = await chromium.launch();
  try {
    const page = await openPage(browser, target, null);

    const before = await runAxe(page, null);
    const currentHash = createHash('sha256').update(await page.content()).digest('hex').slice(0, 16);
    if (args['expect-hash'] && args['expect-hash'] !== currentHash) {
      writeFileSync(args.out, JSON.stringify({
        status: 'stale',
        reason: 'Page content changed since the scan that produced this issue.',
        expected_hash: args['expect-hash'], actual_hash: currentHash
      }));
      console.error('validate: stale');
      return;
    }

    const applied = await page.evaluate(({ sel, html }) => {
      const el = document.querySelector(sel);
      if (!el) return { ok: false, reason: 'selector_not_found' };
      const parent = el.parentElement;
      el.outerHTML = html;
      return { ok: true, parent_tag: parent ? parent.tagName.toLowerCase() : 'body' };
    }, { sel: args.selector, html: patch });

    if (!applied.ok) {
      writeFileSync(args.out, JSON.stringify({
        status: 'not_found',
        reason: `Selector did not match any element: ${args.selector}`
      }));
      console.error('validate: not_found');
      return;
    }

    // Scope to the parent, not the node: rules like color-contrast and
    // aria-required-parent need the surrounding context to evaluate at all.
    const after = await runAxe(page, null);

    const key = (r, n) => `${r}|${n}`;
    const beforeSet = new Set();
    for (const v of before.violations) for (const n of v.nodes) beforeSet.add(key(v.id, n.html));
    const afterList = [];
    for (const v of after.violations) {
      for (const n of v.nodes) {
        afterList.push({ rule: v.id, impact: v.impact, help: v.help,
                         target: Array.isArray(n.target) ? n.target.join(' ') : String(n.target),
                         html: n.html, existed_before: beforeSet.has(key(v.id, n.html)) });
      }
    }

    const targetRule = args.rule || null;
    const remaining = afterList.filter(a => targetRule ? a.rule === targetRule : false);
    const introduced = afterList.filter(a => !a.existed_before && a.rule !== targetRule);

    const beforeCount = before.violations.reduce((s, v) => s + v.nodes.length, 0);
    const afterCount = afterList.length;

    let status;
    if (remaining.length === 0 && introduced.length === 0) status = 'cleared';
    else if (remaining.length === 0 && introduced.length > 0) status = 'regressed';
    else if (remaining.length > 0 && introduced.length === 0) status = 'partial';
    else status = 'regressed';

    writeFileSync(args.out, JSON.stringify({
      status, rule: targetRule, selector: args.selector,
      violations_before: beforeCount, violations_after: afterCount,
      remaining, introduced
    }));
    console.error(`validate: ${status} (remaining=${remaining.length} introduced=${introduced.length})`);
  } finally {
    await browser.close();
  }
}

// ---------------------------------------------------------------- main

const args = parseArgs(process.argv);
try {
  if (args.mode === 'scan') await doScan(args);
  else if (args.mode === 'validate') await doValidate(args);
  else {
    console.error('usage: cli.mjs scan|validate --url <url> --out <file> [...]');
    process.exit(2);
  }
} catch (err) {
  console.error('FATAL', err.message);
  writeFileSync(args.out || 'error.json', JSON.stringify({ status: 'error', reason: err.message }));
  process.exit(1);
}
