// One-time bundler: CodeMirror 6 + markdown editor glue + marked  ->  ../static/vendor/codemirror.bundle.js
// Run from this dir:  npm run build
// Output is an IIFE that attaches window.MoonbaseEditor (offline, no CDN; the PWA service worker caches it).
import * as esbuild from 'esbuild';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const outfile = resolve(__dirname, '../static/vendor/codemirror.bundle.js');

await esbuild.build({
  entryPoints: [resolve(__dirname, 'cm-entry.mjs')],
  bundle: true,
  format: 'iife',
  globalName: '__MoonbaseEditorBundle',
  platform: 'browser',
  target: ['es2019'],
  minify: true,
  sourcemap: false,
  legalComments: 'none',
  outfile,
});

console.log('Built ->', outfile);
