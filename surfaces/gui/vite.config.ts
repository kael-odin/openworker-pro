import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// `base: "./"` makes built asset URLs relative, so the bundle loads from the `tauri://`
// origin in the desktop shell (absolute `/assets` 404s there); a server-hosted build is
// unaffected. Dev runs on a fixed port (1420) with strictPort so the Tauri webview always
// loads the vite instance Tauri itself spawns (a drifting port would make the window load a
// stale/other server). `tauri.conf.json` devUrl must match this.
export default defineConfig(({ command }) => {
  let devToken = "";
  // The token file path — resolved once and reused both for the baked-in define() and for
  // the dev-only `/__dev_token__` middleware below. Sidecar restarts rotate this token; the
  // define() value is frozen at vite startup, but the middleware re-reads on each request so
  // the browser can self-heal after a sidecar restart (see api.ts `refreshDevToken`).
  let devTokenFile = "";
  if (command === "serve") {
    const state =
      process.env.COWORKER_STATE_DIR ||
      (process.platform === "win32"
        ? path.join(process.env.APPDATA || os.homedir(), "coworker")
        : path.join(os.homedir(), ".config", "coworker"));
    devTokenFile = path.join(state, "sidecar-8765.token");
    try {
      devToken = fs.readFileSync(devTokenFile, "utf8").trim();
    } catch {
      // The Tauri dev shell injects its in-memory token at runtime. Plain browser dev
      // shows the normal startup retry until the standalone server/token file exists.
    }
  }
  return {
    base: "./",
    plugins: [
      react(),
      // Dev-only plugin: serve the current sidecar token at /__dev_token__ so the browser can
      // self-heal after a sidecar restart (which rotates the token). The baked-in
      // __COWORKER_DEV_TOKEN__ is frozen at vite startup; this re-reads the file per request.
      // Registered as a plugin (not server.configureServer) so it hooks BEFORE vite's SPA
      // history-fallback middleware, which otherwise serves index.html for unknown routes.
      {
        name: "dev-token-endpoint",
        apply: "serve",
        configureServer(server) {
          // Insert at the FRONT of the middleware stack (unshift, not use) so it runs before
          // Vite's SPA history-fallback, which otherwise rewrites unknown URLs to index.html.
          // (configureServer's return-a-function hook supposedly does this, but in practice the
          // use()-appended middleware lands after the fallback — verified empty on Vite 5.4.)
          const handler = (req: any, res: any, next: any) => {
            const u = (req.url || "").split("?")[0];
            if (u !== "/__dev_token__.json") return next();
            try {
              const t = fs.readFileSync(devTokenFile, "utf8").trim();
              res.setHeader("Content-Type", "application/json");
              res.end(JSON.stringify({ token: t }));
            } catch {
              res.statusCode = 404;
              res.end(JSON.stringify({ token: "" }));
            }
          };
          // `use` appends at the end (too late); splice into position 0 to run first.
          server.middlewares.stack.splice(0, 0, { route: "", handle: handler });
        },
      },
    ],
    server: {
      port: 1420,
      strictPort: true,
      // The Tauri dev flow runs `cargo build` in src-tauri/target while Vite's
      // fs watcher is live; on Windows the build-script .exe files there are
      // locked by cargo, so a recursive watch into target/ hits EBUSY and
      // crashes Vite (killing beforeDevCommand → Tauri aborts). Keep the
      // watcher out of cargo's build tree (and the Python venv) entirely.
      watch: {
        ignored: [
          "**/src-tauri/target/**",
          "**/.venv/**",
          "**/node_modules/**",
        ],
      },
    },
    define: { __COWORKER_DEV_TOKEN__: JSON.stringify(devToken) },
    // Tauri CLI looks for these; harmless for the browser build.
    clearScreen: false,
    envPrefix: ["VITE_", "TAURI_"],
  };
});
