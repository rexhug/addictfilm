import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./frontend/e2e",
  timeout: 20_000,
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  projects: [{
    name: "mobile-chromium",
    // Keep the real 390px mobile geometry while using Chromium everywhere,
    // including CI.  The iPhone device descriptor otherwise selects WebKit.
    use: { ...devices["iPhone 13"], browserName: "chromium" },
  }],
  webServer: {
    command: "python3 -m http.server 4173 --directory frontend",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
  },
});
