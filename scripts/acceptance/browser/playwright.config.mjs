import { defineConfig } from '@playwright/test'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const output = path.resolve(process.env.TSP_BROWSER_OUTPUT || path.join(os.tmpdir(), 'tsp-browser-acceptance'))
const python = process.env.TSP_TEST_PYTHON || path.join(root, 'backend/.venv/bin/python')
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'"

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.mjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  outputDir: path.join(output, 'results'),
  reporter: [
    ['list'],
    ['json', { outputFile: path.join(output, 'results.json') }],
    ['junit', { outputFile: path.join(output, 'junit.xml') }],
  ],
  use: {
    baseURL: 'http://127.0.0.1:39019',
    browserName: 'chromium',
    headless: true,
    actionTimeout: 15_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'narrow', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: `${quote(python)} -m scripts.acceptance.accept_local_web --serve --port 39019`,
    cwd: path.join(root, 'backend'),
    url: 'http://127.0.0.1:39019/api/auth/status',
    reuseExistingServer: false,
    timeout: 90_000,
    env: {
      TICKFLOW_ENV_FILE: '/dev/null',
      DATA_DIR: path.join(output, 'bootstrap-data'),
      MULTITENANCY_ENABLED: 'false',
      SERVICE_ROLE: 'standalone',
      POLARS_MAX_THREADS: '2',
      PYTHONDONTWRITEBYTECODE: '1',
    },
  },
})
