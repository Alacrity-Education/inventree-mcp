// Loads every built bundle with the host globals stubbed and checks the exports.
// Usage: npm run build && npm run check:bundles
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import React from 'react';
import { renderToString } from 'react-dom/server';
import * as MantineCore from '@mantine/core';
import * as MantineNotifications from '@mantine/notifications';

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, '..', '..', 'inventree_mcp', 'static');

// Globals the InvenTree host provides
globalThis.window = globalThis;
globalThis.React = React;
globalThis.ReactDOM = {};
globalThis.MantineCore = MantineCore;
globalThis.MantineNotifications = MantineNotifications;
globalThis.LinguiCore = {};
globalThis.LinguiReact = {};
globalThis.location = { origin: 'http://localhost:8000' };

const expected = {
  Groups: 'renderGroupsPage',
  Connections: 'renderConnectionsPage',
  Panel: 'renderMCPRolesPanel'
};

const files = readdirSync(staticDir).filter((f) => f.endsWith('.js'));
let failed = false;

// A context whose API never answers: components must still render (loading state).
const context = {
  version: { inventree: '1.5.0' },
  api: { get: () => new Promise(() => {}), post: () => new Promise(() => {}), patch: () => new Promise(() => {}), delete: () => new Promise(() => {}) },
  user: { isStaff: () => true, userId: () => 1, username: () => 'admin' },
  navigate: () => {},
  id: 1
};

for (const [name, fn] of Object.entries(expected)) {
  const candidates = files.filter((f) => f === `${name}.js` || new RegExp(`^${name}-[\\w-]+\\.js$`).test(f));
  if (candidates.length < 2) {
    console.log(`FAIL ${name}: expected plain and hashed file, found ${candidates.join(', ') || 'none'}`);
    failed = true;
    continue;
  }
  for (const file of candidates) {
    const mod = await import(pathToFileURL(join(staticDir, file)).href);
    const ok = typeof mod[fn] === 'function';
    let rendered = '';
    if (ok) {
      try {
        rendered = renderToString(
          React.createElement(MantineCore.MantineProvider, null, mod[fn](context))
        );
      } catch (e) {
        console.log(`  render error: ${e.message}`);
      }
    }
    const status = ok && rendered ? 'ok' : 'FAIL';
    if (status === 'FAIL') failed = true;
    console.log(`${status} ${file}: exports [${Object.keys(mod).join(', ')}], renders ${rendered.length} bytes of HTML`);
  }
}
process.exit(failed ? 1 : 0);
