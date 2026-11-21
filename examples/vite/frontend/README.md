# frontend

The Vue frontend of `examples/vite`, made with [Vite+](https://viteplus.dev) (`vp create vue -- frontend --ts --router`).
The server in `../src/main.cee` serves it on the API's own origin (`httpserver_vite`), so `fetch("/api/hello")` needs
no CORS and no `server.proxy` in `vite.config.ts`.

```sh
vp install                                   # once
cd .. && ceesafec build
HTTPSERVER_FRONTEND=dev out/vite_app         # development: the server starts `vp dev` and proxies to it, HMR included
(cd frontend && vp build) && out/vite_app    # built: the server serves frontend/dist
```

Open http://127.0.0.1:8080 either way. `vp check` lints, formats and type-checks the frontend; `vp dev` alone runs
Vite's server without the API.
