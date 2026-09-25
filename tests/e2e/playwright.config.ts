import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.WALLIA_E2E_BASE_URL ?? "http://127.0.0.1:13745";

export default defineConfig({
  testDir: "./specs",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "off",
    screenshot: "off",
    video: "off",
    ignoreHTTPSErrors: false,
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile",
      use: { ...devices["iPhone 13"] },
    },
  ],
});
