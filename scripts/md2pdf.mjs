/**
 * Render a Markdown file to PDF with Mermaid diagrams.
 *
 *   node scripts/md2pdf.mjs DESIGN.md DESIGN.pdf
 *
 * Reuses the Playwright chromium the scanner already installed, so there is no
 * extra toolchain. Mermaid renders in the page before printing.
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { chromium } from '../scanner/node_modules/playwright/index.mjs';
import { marked } from '../scanner/node_modules/marked/lib/marked.esm.js';

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
  console.error('usage: node scripts/md2pdf.mjs <in.md> <out.pdf>');
  process.exit(2);
}

const md = readFileSync(inPath, 'utf8');

// Pull mermaid fences out before marked touches them, then re-insert as <pre class="mermaid">.
const blocks = [];
const prepared = md.replace(/```mermaid\n([\s\S]*?)```/g, (_, code) => {
  blocks.push(code);
  return `\n@@MERMAID${blocks.length - 1}@@\n`;
});

let html = marked.parse(prepared);
html = html.replace(/<p>@@MERMAID(\d+)@@<\/p>/g,
  (_, i) => `<pre class="mermaid">${blocks[Number(i)]}</pre>`);

const page_html = `<!doctype html>
<html><head><meta charset="utf-8">
<style>
  @page { size: A4; margin: 12mm 13mm; }
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body { font: 9pt/1.36 "Segoe UI", system-ui, sans-serif; color:#16191d; margin:0; }
  h1 { font-size: 15pt; margin: 0 0 .35em; }
  h2 { font-size: 11pt; margin: .6em 0 .25em; padding-top:.2em;
       border-top: 1px solid #dfe3e8; }
  h1 + p, h2 + p { margin-top: .25em; }
  p { margin: .32em 0; }
  ul, ol { margin: .35em 0; padding-left: 1.15em; }
  li { margin: .12em 0; }
  code { font-family: ui-monospace, Consolas, monospace; font-size: .87em;
         background: #f2f4f7; padding: .05em .25em; border-radius: 2px; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: .45em 0; font-size: 8.4pt; }
  th, td { border: 1px solid #dfe3e8; padding: .22em .42em; text-align: left;
           vertical-align: top; }
  th { background: #f6f7f9; font-weight: 600; }
  hr { display: none; }
  blockquote { margin: .4em 0; padding-left: .7em; border-left: 2px solid #dfe3e8;
               color: #5b6470; }
  .mermaid { text-align: center; margin: .35em 0; background: none; }
  .mermaid svg { max-width: 100%; height: auto; }
  h2, h3 { break-after: avoid; }
  table, .mermaid { break-inside: avoid; }
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
</head><body>
${html}
<script>
  mermaid.initialize({ startOnLoad: false, theme: 'neutral',
                       themeVariables: { fontSize: '12px' },
                       flowchart: { htmlLabels: true, curve: 'basis' } });
  window.__ready = mermaid.run().then(() => true).catch(e => { console.error(e); return true; });
</script>
</body></html>`;

const dir = mkdtempSync(join(tmpdir(), 'md2pdf-'));
const htmlPath = join(dir, 'doc.html');
writeFileSync(htmlPath, page_html, 'utf8');

const browser = await chromium.launch();
const page = await browser.newPage();
await page.goto(pathToFileURL(htmlPath).href, { waitUntil: 'networkidle' });
await page.waitForFunction('window.__ready !== undefined');
await page.evaluate(() => window.__ready);
await page.waitForTimeout(600);

await page.pdf({ path: outPath, format: 'A4', printBackground: true });

// Report the page count so length can be tuned against reality, not guessed.
const height = await page.evaluate(() => document.body.scrollHeight);
await browser.close();

const { execSync } = await import('node:child_process');
console.log(`wrote ${outPath}`);
console.log(`content height: ${height}px (A4 printable ~1010px/page at this margin)`);
console.log(`estimated pages: ${(height / 1010).toFixed(2)}`);
