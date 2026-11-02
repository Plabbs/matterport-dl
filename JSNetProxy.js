
const _hostRegex = new RegExp( /(https?:\/\/[^/]+)/,"i");
window._replaceHost = function(str){
	if (! str)
		return str;
	if (window._ProxyAppendURL) {
		const encodedOrig = str;
		if (str.includes("?"))
			str +="&";
		else
			str +="?";
		str += "__OU=" + encodeURIComponent(encodedOrig);
	}

	if (window._NoTilde)
		str = str.replace("~","_")
	return str.replace(_hostRegex,window._ProxyBase);
}
window.nv_XMLHttpRequest = new Proxy(XMLHttpRequest, {
	construct: function (target, args) {
		const originalRequest = new target();
		const prototypeDescriptors = Object.getOwnPropertyDescriptors(
			target.prototype
		)
		for (const propertyName in prototypeDescriptors) {
			Reflect.defineProperty(
				originalRequest,
				propertyName,
				prototypeDescriptors[propertyName]
			)
		}
		return new Proxy(originalRequest, {
			get: (target, name, trap) => {
				
				
				if (typeof target[name] === 'function') {
				  return (...args) => {
					switch (name) {
					  case 'open':
						if (args.length > 1){
							args[1] = window._replaceHost(args[1]);
							target.__mdUrl = args[1];
						}
						break;

					  case 'send':
						if (window._trackRequest){
							if (!target.__mdTracked){
								target.__mdTracked = true;
								target.addEventListener('loadend', () => window._trackDone(target.__mdToken, target.status >= 400 || target.status === 0));
							}
							target.__mdToken = window._trackRequest(target.__mdUrl || 'xhr');
						}
						break;
		
					  default:
						break;
					}
		
					return target[name].apply(target, args);
				  }
				}
		
				
				return target[name];
			  },
			set: function (target, prop, value) {
				Reflect.set(target, prop, value) // or target[prop] = value
				return true;
			},
		})
	}
})

var oReq = new XMLHttpRequest();

window.nv_fetch = new Proxy(window.fetch, {
	apply: function (target, that, args) {
		let trackUrl = null;
		if (args.length > 0 && args[0])
			if (typeof args[0] !== 'string' && args[0].url){
				const newUrl = window._replaceHost( args[0].url );
				if (newUrl != args[0].url)
					args[0] = new Request(newUrl, args[0]);
				trackUrl = newUrl;
			}
			else{
				args[0] = window._replaceHost(args[0].toString());
				trackUrl = args[0];
			}
		const token = trackUrl && window._trackRequest ? window._trackRequest(trackUrl) : null;
		const result = target.apply(that, args);
		if (token && result && result.then)
			return result.then((r) => { window._trackDone(token, r && r.ok === false); return r; },
				(e) => { window._trackDone(token, true); throw e; });
		return result;
	},
});

window.XMLHttpRequest = window.nv_XMLHttpRequest;
window.fetch = window.nv_fetch;
window.oldAppendChild = Element.prototype.appendChild;
Element.prototype.appendChild = function() {
	if (arguments.length > 0) {
		if ( (arguments[0]?.tagName == "SCRIPT" || arguments[0]?.tagName == "IMG") && arguments[0].src) {
			arguments[0].src = window._replaceHost(arguments[0].src);
			// only scripts are tracked: they always end with load or error, images can be
			// thrown away half way and would look like a request that never finishes
			if (window._trackRequest && arguments[0].tagName == "SCRIPT" && !arguments[0].__mdTracked) {
				arguments[0].__mdTracked = true;
				const token = window._trackRequest(arguments[0].src);
				arguments[0].addEventListener("load", () => window._trackDone(token, false), { once: true });
				arguments[0].addEventListener("error", () => window._trackDone(token, true), { once: true });
			}
		}
		else if ( arguments[0]?.tagName == "DIV" && arguments[0].style?.backgroundImage?.startsWith("url"))
			arguments[0].style.backgroundImage = window._replaceHost(arguments[0].style?.backgroundImage);
	}
    return window.oldAppendChild.apply(this, arguments);
};

console.log("PROXY IN PLACE");

/*
For react you may run into an issue:
reactCont=reactCont.replace("(t.src=s.src)","(t.src=\"\"+(t.src??s.src))") # hacky but in certain conditions react will try to reset the source on something after it loads to re-trigger the load event but this breaks jsnetproxy.  This allows the same triggering but uses the existing source if it exists.  https://github.com/facebook/react/blob/37906d4dfbe80d71f312f7347bb9ddb930484d28/packages/react-dom-bindings/src/client/ReactFiberConfigDOM.js#L744

we could check it after load but thats a bit of a pita can't seem to override the src attribute to make it read only or anything.  we could copy it but if it needs the handle to it that would break it.
*/

/* ---- stuck loading watchdog ---------------------------------------------
   The showcase keeps #loading-gui (the circle spinner) up until the model is
   ready.  When that never happens the only way out has been to reload, so when
   that happens we now: say what is still in flight (console + the servers
   server.log via POST /__diag) and reload the page once per session.
   window._NoStuckReload = true keeps the reporting but skips the reload. */
(function () {
	const reportAfterMs = 15000, quietMs = 6000, inflightStallMs = 8000;
	const reloadAgeMs = 25000, reloadQuietMs = 12000, reloadInflightMs = 12000, giveUpAfterMs = 180000;
	const pageStart = Date.now(), reloadKey = "matterport-dl-stuck-reload";
	let inflight = {}, errors = [], lastActivity = Date.now(), reported = false, interacted = false, reloadArmed = false;

	function eachInflight(maxAge) {
		const now = Date.now(), out = [];
		for (const token in inflight) {
			const item = inflight[token], age = now - item.start;
			if (age >= maxAge) out.push({ url: String(item.url).slice(0, 150), ms: Math.round(age) });
		}
		out.sort((a, b) => b.ms - a.ms);
		return out.slice(0, 10);
	}
	window._trackRequest = function (url) {
		const token = "" + Math.random();
		inflight[token] = { url: url, start: Date.now() };
		lastActivity = Date.now();
		return token;
	};
	window._trackDone = function (token, failed) {
		if (inflight[token]) {
			if (failed) errors.push("failed: " + inflight[token].url);
			delete inflight[token];
		}
		lastActivity = Date.now();
	};
	window.addEventListener("error", (e) => { if (errors.length < 20) errors.push("error: " + (e.message || e.type) + " @ " + (e.filename || "?") + ":" + (e.lineno || "?")); }, true);
	window.addEventListener("unhandledrejection", (e) => { if (errors.length < 20) errors.push("rejected: " + ((e.reason && (e.reason.message || e.reason.stack || e.reason)) || "unknown")); });
	// A click or scroll on the full-screen loading gui is how you try to start a stuck tour.
	// That must not cancel the one reload that gets the page out of the spinner.
	function noteInteraction(e) {
		if (loadingGuiUp()) return;
		const gui = document.getElementById("loading-gui");
		if (gui && e && e.target && gui.contains && gui.contains(e.target)) return;
		interacted = true;
	}
	["pointerdown", "keydown", "wheel"].forEach((ev) => window.addEventListener(ev, noteInteraction, { passive: true }));

	function loadingGuiUp() {
		const gui = document.getElementById("loading-gui");
		if (!gui || gui.classList.contains("faded-out")) return false;
		const cs = window.getComputedStyle(gui);
		return cs.display !== "none" && cs.visibility === "visible" && parseFloat(cs.opacity || "1") > 0.05;
	}
	function snapshot(reason) {
		return {
			reason: reason, pageSeconds: (Date.now() - pageStart) / 1000,
			inflightCount: Object.keys(inflight).length, oldestInflight: eachInflight(0)[0] || null,
			stalledInflight: eachInflight(inflightStallMs), recentErrors: errors.slice(-8),
			secondsSinceLastRequest: (Date.now() - lastActivity) / 1000, loadingGuiUp: loadingGuiUp(),
			hasCanvas: !!document.querySelector("#canvas-container canvas"), tabHidden: !!document.hidden,
		};
	}
	function tellServer(s) {
		console.warn("[matterport-dl] viewer stuck: " + JSON.stringify(s, null, 1));
		try { window.fetch("/__diag", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(s) }); } catch (e) { }
	}
	function tick() {
		const age = Date.now() - pageStart;
		if (age < reportAfterMs || !loadingGuiUp()) return;
		const s = snapshot("loading-gui still up");
		const quietFor = s.secondsSinceLastRequest * 1000, oldest = s.oldestInflight ? s.oldestInflight.ms : 0;
		if (!reported && (s.stalledInflight.length > 0 || (s.inflightCount === 0 && quietFor > quietMs))) { reported = true; tellServer(s); }
		// Reload only once the page has clearly had its chance: something hung half way through a
		// transfer or the network went completely quiet, either way nothing is making progress
		if (age > reloadAgeMs && (oldest > reloadInflightMs || quietFor > reloadQuietMs) && !window._NoStuckReload && !interacted && !document.hidden && !reloadArmed) {
			let reloaded = null;
			try { reloaded = sessionStorage.getItem(reloadKey); } catch (e) { }
			if (!reloaded) {
				// Set this before storage. If sessionStorage throws, a later tick must not reload again.
				reloadArmed = true;
				window._matterportDlStuckReloaded = true;
				try { sessionStorage.setItem(reloadKey, JSON.stringify(s)); } catch (e) { }
				console.warn("[matterport-dl] reloading the page once to get past the stuck load");
				setTimeout(() => location.reload(), 250);
			}
		}
		if (age > giveUpAfterMs) clearInterval(timer);
	}
	try {
		const before = sessionStorage.getItem(reloadKey);
		if (before) setTimeout(() => tellServer({ reason: "reloaded earlier this session because", previous: JSON.parse(before) }), 3000);
	} catch (e) { }
	const timer = setInterval(tick, 1000);
})();
