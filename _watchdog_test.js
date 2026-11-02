/* Runs JSNetProxy.js against a stubbed browser so the stuck load watchdog can be exercised
   without a real browser: does it report when the loading gui never goes away, does it reload,
   and does it stay quiet on a page that loads fine.  Run with: node _watchdog_test.js */

const fs = require("fs");
const vm = require("vm");

let fails = 0;
const check = (name, cond, extra) => { console.log((cond ? "PASS " : "FAIL ") + name + (extra ? " :: " + extra : "")); if (!cond) fails++; };

let clock = 0;                                   // the fake Date.now() the proxy sees
let guiUp = true;
const posts = [], warns = [], timeouts = [];
let reloaded = false, stored = null, tickFn = null;

function FakeElement(tag) { this.tagName = tag; this.listeners = {}; }
FakeElement.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
FakeElement.prototype.fire = function (t) { (this.listeners[t] || []).forEach((f) => f()); };

const gui = new FakeElement("DIV");
gui.classList = { contains: () => false };

const context = {
  console: { log: () => { }, warn: (m) => warns.push(m) },
  Date: { now: () => clock },
  setInterval: (fn) => { tickFn = fn; return 1; },
  clearInterval: () => { },
  setTimeout: (fn) => { timeouts.push(fn); return 1; },
  addEventListener: (type, fn) => { (context._listeners[type] = context._listeners[type] || []).push(fn); },
  _listeners: {},
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  location: { reload: () => { reloaded = true; } },
  sessionStorage: { getItem: () => stored, setItem: (k, v) => { stored = v; } },
  navigator: { userAgent: "node" },
  XMLHttpRequest: function () { this.status = 200; this.listeners = {}; },
  Element: function () { },
  fetch: (url, opts) => { posts.push({ url: url, method: (opts || {}).method || "GET" }); return Promise.resolve({ ok: true }); },
  document: {
    hidden: false,
    getElementById: (id) => (id === "loading-gui" && guiUp ? gui : null),
    querySelector: () => null,
  },
};
context.XMLHttpRequest.prototype = {};
context.window = context;
context.Element.prototype.appendChild = function (el) { return el; };

vm.runInNewContext(fs.readFileSync("JSNetProxy.js", "utf8"), context, { filename: "JSNetProxy.js" });
context._ProxyBase = "http://127.0.0.1:8123";

const settle = () => new Promise((r) => setImmediate(() => setImmediate(r)));

(async () => {
  check("watchdog installed", typeof context._trackRequest === "function" && typeof context._trackDone === "function");
  check("interval armed", typeof tickFn === "function");

  // a proxied fetch is rewritten and tracked, then cleared when it settles
  const before = posts.length;
  const p = context.window.fetch("https://static.matterport.com/some/data.json");
  check("fetch still proxied to us", posts[before] && posts[before].url === "http://127.0.0.1:8123/some/data.json", JSON.stringify(posts[before]));
  await settle();

  // an appended script is tracked until it loads
  const script = new FakeElement("SCRIPT");
  script.src = "https://asset.matterport.com/js/chunk.js";
  context.Element.prototype.appendChild.call({}, script);
  check("appended script src proxied", script.src === "http://127.0.0.1:8123/js/chunk.js", script.src);

  // 16 seconds in with the loading gui up and one script that never finished
  clock = 16000;
  tickFn();
  const stuckPost = posts.find((p) => p.url === "/__diag" && p.method === "POST");
  check("reports a stuck load to /__diag", !!stuckPost, JSON.stringify(posts.slice(-2)));
  const report = warns.find((w) => w.includes("viewer stuck")) || "";
  check("reports it in the console too", !!report, warns.join(" | ").slice(0, 120));
  check("report names the script that never loaded", report.includes("chunk.js") && report.includes('"inflightCount": 1'), report.replace(/\s+/g, " ").slice(0, 220));
  check("report does not blame the settled fetch", !report.includes("data.json"));
  check("does not reload before 25s", reloaded === false);

  // 26 seconds in, still nothing, now it should reload the page for us
  clock = 26000;
  tickFn();
  timeouts.forEach((f) => f());
  check("reloads the page once stuck", reloaded === true);
  check("remembers it reloaded", !!stored && stored.includes("loading-gui"));

  // once the gui is gone the watchdog has nothing to say
  const postsSoFar = posts.length, warnsSoFar = warns.length;
  guiUp = false;
  clock = 40000;
  tickFn();
  check("quiet once the viewer is up", posts.length === postsSoFar && warns.length === warnsSoFar);

  function loadProxy(storage) {
    const local = { clock: 0, reloaded: false, timeouts: [], listeners: {} };
    const pageGui = new FakeElement("DIV");
    pageGui.classList = { contains: () => false };
    pageGui.contains = (node) => node === pageGui;
    const page = {
      console: { log: () => { }, warn: () => { } },
      Date: { now: () => local.clock },
      setInterval: (fn) => { local.tick = fn; return 1; },
      clearInterval: () => { },
      setTimeout: (fn) => { local.timeouts.push(fn); return 1; },
      addEventListener: (type, fn) => { (local.listeners[type] = local.listeners[type] || []).push(fn); },
      getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
      location: { reload: () => { local.reloaded = true; } },
      sessionStorage: storage,
      navigator: { userAgent: "node" },
      XMLHttpRequest: function () { this.status = 200; this.listeners = {}; },
      Element: function () { },
      fetch: () => Promise.resolve({ ok: true }),
      document: {
        hidden: false,
        getElementById: () => pageGui,
        querySelector: () => null,
      },
    };
    page.XMLHttpRequest.prototype = {};
    page.window = page;
    page.Element.prototype.appendChild = function (el) { return el; };
    vm.runInNewContext(fs.readFileSync("JSNetProxy.js", "utf8"), page, { filename: "JSNetProxy.js" });
    return { local, pageGui };
  }

  {
    const storage = { getItem: () => null, setItem: () => { throw new Error("blocked"); } };
    const { local, pageGui } = loadProxy(storage);
    (local.listeners.pointerdown || []).forEach((fn) => fn({ target: pageGui }));
    local.clock = 26000;
    local.tick();
    local.tick();
    local.timeouts.forEach((fn) => fn());
    check("click on the loading screen still reloads once", local.reloaded === true && local.timeouts.length === 1);
  }

  console.log(fails ? `\nFAILURES: ${fails}` : "\nALL GOOD");
  process.exit(fails ? 1 : 0);
})();
