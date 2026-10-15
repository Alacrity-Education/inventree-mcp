import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { viteExternalsPlugin } from 'vite-plugin-externals';

/**
 * Libraries provided by the InvenTree host application (window globals).
 * They must not be bundled with the plugin.
 */
export const externalLibs: Record<string, string> = {
  react: 'React',
  'react-dom': 'ReactDOM',
  ReactDom: 'ReactDOM',
  '@lingui/core': 'LinguiCore',
  '@lingui/react': 'LinguiReact',
  '@mantine/core': 'MantineCore',
  '@mantine/notifications': 'MantineNotifications'
};

const externalKeys = Object.keys(externalLibs);
const outDir = '../inventree_mcp/static';

export default defineConfig({
  plugins: [
    react({ jsxRuntime: 'classic' }),
    viteExternalsPlugin(externalLibs)
  ],
  esbuild: {
    jsx: 'preserve'
  },
  build: {
    target: 'esnext',
    cssCodeSplit: false,
    manifest: true,
    sourcemap: true,
    rollupOptions: {
      preserveEntrySignatures: 'exports-only',
      input: ['./src/Groups.tsx', './src/Connections.tsx', './src/Panel.tsx'],
      output: [
        // Plain names: stable, for backwards compatibility
        {
          dir: outDir,
          entryFileNames: '[name].js',
          assetFileNames: 'assets/[name].[ext]',
          globals: externalLibs
        },
        // Hashed names: cache busting (resolved by core's plugin_static_file)
        {
          dir: outDir,
          entryFileNames: '[name]-[hash].js',
          assetFileNames: 'assets/[name].[ext]',
          globals: externalLibs
        }
      ],
      external: externalKeys
    }
  },
  optimizeDeps: {
    exclude: externalKeys
  }
});
