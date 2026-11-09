import { defineConfig } from "@playwright/test";
import path from "node:path";

const python =
  process.env.FRIDA_PYTHON ||
  path.resolve(
    "../../.venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.js",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: { channel: "chrome", headless: true, trace: "off" },
  projects: [
    {
      name: "desktop",
      use: {
        baseURL: "http://127.0.0.1:18761",
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "phone",
      use: {
        baseURL: "http://127.0.0.1:18762",
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
  webServer: [18761, 18762].map((port) => ({
    command: `"${python}" -m uvicorn dashboard_api_preview:create_preview --factory --app-dir "${path.resolve("../../tests")}" --host 127.0.0.1 --port ${port}`,
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: false,
  })),
});
