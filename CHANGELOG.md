# 2026-11-02
- Show-page `validUntil` values stored as escaped JSON (`\"validUntil\":\"2026-...`) are rewritten to 2099 along with plain JSON, so signatures baked into the HTML do not expire out from under the local viewer.
- An HTTP 429 waits and retries up to 5 more times, backing off 2, 4, 8, 16, then 32 seconds. A 404 is not retried.
- Graph download also requests `GetModelAssets` and `GetSweeps` (persisted-query hashes captured from the live showcase).
- An alias made from the interactive rename is a symlink whose target is the model id beside it. Serving `python matterport-dl.py "<alias>" 127.0.0.1 8080` opens that folder.

# 2026-10-23
- Fixed the viewer sometimes hanging on the loading spinner (only a page reload got you out of it).  The local server now answers every browser connection on its own thread with http/1.1 keep alive (was one connection at a time with a backlog of 5, so requests the viewer was waiting on could go unserved while the browser sat on an idle connection), always sends a complete response for graph api requests (an operationName we don't know used to answer with nothing at all, leaving the showcase waiting on that promise forever), consumes post bodies, and sends a content length on every response.
- The 30 second keep alive timeout only applies while waiting for the next request on a connection now, it can no longer cut a large asset (a 5 MB mesh tile for example) off half way through handing it to a browser that is momentarily busy.
- When the viewer does get stuck the reason gets recorded instead of just being worked around: the watchdog in `JSNetProxy.js` says what the page is still waiting on (console plus a `POST /__diag` that lands in `downloads/<model>/server.log`) and then reloads the page once on its own, and you can ask the server the same question yourself at `GET /__diag` (it lists what is in flight, what it served last, and anything that took over 8 seconds).  Set `window._NoStuckReload = true` to keep the reporting but skip the automatic reload.
- Support for the newer (2026+) showcase builds: the webpack runtime no longer contains a per chunk key (hash) map so chunk names are now `js/<name>.js`, and the chunk ids are discovered from the chunk load calls (`.e(<id>)`) and the async import maps (`{"./file.svg":[8249,2622]}`) in the downloaded js rather than from a map in the runtime js.
- Static files the downloaded js/css reference that aren't in the hardcoded lists (such as `images/atlas.png` and the new `cursors/rbe-*.png`) are now found and downloaded as well.
- Plugin manifests with a build number in the version (compass `1.0.14-1`) now also download the version the player actually requests (`1.0.14`).
- Overlay cube-face tiles are downloaded from the signed URLs embedded in the show page, before the primary sweep walk, with `t=` and `k=` left as Matterport sent them. The `GetShowcaseSweeps` copy of those URLs expires within minutes, and spending that time checking tiles already on disk made every request come back 410. Locked resolutions (often 4k) are still skipped. A re-run no longer ticks through each existing file.
- A missing file under `/models/` is still answered with a completed stand-in so the viewer can move on, but that stand-in is `Cache-Control: no-store` and `GET /__diag` lists it under `missingAssets`. Real 404s stay on the keep-alive connection. A second copy of the server can no longer bind the same port. The stuck-page reload ignores clicks on the loading screen, and it still reloads only once when `sessionStorage` is blocked. Webpack import maps with more than one chunk id are downloaded in full.

# 2025-03-25
- Added `run.py` and recommend as the default entrypoint.  It should do better to make sure everyone has the proper requirements and python version installed.

# 2025-03-22
- Major new interactive terminal UI mostly thanks to @AdiWeit. Allows downloading multiple urls at once, extracting matterport embeds, renaming, deleting, and launching models from the interface

# 2025-01-24
- Added embedded attachment / 'matterport tags' downloading for things like image points

## 2025-01-10
- Local cropped mesh_tile image generation leads to much higher dollhouse views and fixes some texture errors

## 2024-12-30
- Basic defurnished model support, nearly all excess 403/404 requests eliminated

## 2024-12-25
- Major changes to frontend from matterport including stricter access key requirements, access keys for javascript files, and multiple other changes.  Major restructuring of matterport-dl.py to avoid brute force methods and properly harvest nearly all resource data we need to download.  No longer using every access key we know if failure, but instead using the correct access key per resource.  This should speed up runs a good bit by cutting the # of requests down.

## 2024-09-14
- Downloading models from behind the great china firewall added through their matterportvr.cn server

## 2024-08-19
- New auto-serve option to start server and optionally launch browser (with fix for Windows browsers)
- Sample [Auto PY to EXE](https://github.com/brentvollebregt/auto-py-to-exe) config added for standalone executable generation
- `defaults.json` support for default command line options

## 2024-08-10
- Disable SSL verification for easier proxy use
- Extended encoding support (matterport unicode scaping embedded JS on non-english pages)
- Download static files/photos using graphql data as old keys not reliable
- New expiry code defeat and fix some expiry dates also not replaced


## 2024-08-03
- JS network proxy to leave most code unmodified but still redirect
- Downloading of plugins and other advanced matterport features like floor plan sq/ft and other settings
- VR support
- Lower cpu usage during initial capture
- Ability to set the directory for downloads
- Further download progress details
- No-tilde in path to assist non-windows platform downloads
- Save copy of matterport-dl.py and the JS Proxy next to the model for enhanced compatibility.
