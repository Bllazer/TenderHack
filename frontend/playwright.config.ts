import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests', fullyParallel: false, workers: 1,
  use: { baseURL: 'http://127.0.0.1:5174', browserName: 'chromium' },
  webServer: [
    { command: `"${process.env.BACKEND_TEST_PYTHON || 'python'}" tests/browser_server.py`, cwd: '../backend',
      url: 'http://127.0.0.1:18001/api/health/live', reuseExistingServer: false, timeout: 60000 },
    { command: 'pnpm dev --host 127.0.0.1 --port 5174', env: { API_PROXY_TARGET: 'http://127.0.0.1:18001' },
      url: 'http://127.0.0.1:5174', reuseExistingServer: false },
  ],
})
