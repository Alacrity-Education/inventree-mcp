import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Tests do not use the externals plugin: React and Mantine resolve from
// node_modules, which is exactly what the externals map to at runtime.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.tsx', 'src/**/*.test.ts'],
    setupFiles: ['./src/test/setup.ts']
  }
});
