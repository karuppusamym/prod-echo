// Observe-only listener injected by the CDP recorder with Page.addScriptToEvaluateOnNewDocument
// (and evaluated once in documents that were already open when the recorder attached). It never
// changes the page: it listens to click/input/change in the capture phase, counts in-flight
// fetch/XHR so it knows when the page has settled, and reports through the Runtime binding
// __echoEmit. Element states "before" an action are the states at the last settled point; "after"
// states are taken once the page is quiet again.
//
// It runs in every frame. Each frame reports its own document; the recorder knows which frame a
// report came from (by execution context) and composes the frames into the top-level screen.
// Open shadow roots are walked: their elements are tracked and keyed `<host key> >>> <inner key>`,
// and the snapshot HTML carries them as declarative shadow DOM (<template shadowrootmode="open">).
// Content with no HTML to read (canvas, embedded PDF/plugin objects) is listed as `opaque` with its
// box, so the recorder can crop it from the screenshot.
(function () {
  "use strict";
  if (window.__echoInstalled) return;
  window.__echoInstalled = true;
  var QUIET_MS = 200, MAX_MS = 6000;
  var pending = 0, lastMutation = Date.now(), lastSettled = null, cur = null, seq = 0;
  var loadSettled = false;
  var observed = new WeakSet();

  function emit(obj) {
    try { if (window.__echoEmit) window.__echoEmit(JSON.stringify(obj)); } catch (e) { /* ignore */ }
  }

  // ---- in-flight request counting (wrappers call through unchanged)
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function () {
      pending++;
      var p = origFetch.apply(this, arguments);
      var done = function () { pending = Math.max(0, pending - 1); lastMutation = Date.now(); };
      p.then(done, done);
      return p;
    };
  }
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    pending++;
    this.addEventListener("loadend", function () { pending = Math.max(0, pending - 1); lastMutation = Date.now(); });
    return origSend.apply(this, arguments);
  };

  // ---- document + open shadow roots
  function roots() {
    var out = [document], i = 0;
    while (i < out.length) {
      var all = out[i++].querySelectorAll("*");
      for (var j = 0; j < all.length; j++) if (all[j].shadowRoot) out.push(all[j].shadowRoot);
    }
    return out;
  }

  function queryAll(sel) {
    var out = [];
    roots().forEach(function (r) { Array.prototype.push.apply(out, r.querySelectorAll(sel)); });
    return out;
  }

  function hostOf(el) {
    var r = el.getRootNode ? el.getRootNode() : document;
    return r && r.host ? r.host : null;
  }

  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
      var tag = el.tagName.toLowerCase(), i = 1, sib = el;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === el.tagName) i++;
      var cls = el.classList.length ? "." + Array.prototype.map.call(el.classList, CSS.escape).join(".") : "";
      parts.unshift(tag + cls + ":nth-of-type(" + i + ")");
      el = el.parentElement;  // stops at a shadow root: the path is relative to it
    }
    return parts.join(" > ");
  }

  function localKey(el) {
    if (el.id) return "#" + el.id;
    var root = el.getRootNode ? el.getRootNode() : document;
    if (el.name && root.querySelectorAll('[name="' + CSS.escape(el.name) + '"]').length === 1) {
      return "[name=" + el.name + "]";
    }
    return cssPath(el);
  }

  // `#host >>> #inner` for an element inside an open shadow root (nested roots chain)
  function keyOf(el) {
    var h = hostOf(el);
    return h ? keyOf(h) + " >>> " + localKey(el) : localKey(el);
  }

  function cssOf(el) {
    var h = hostOf(el);
    return h ? cssOf(h) + " >>> " + cssPath(el) : cssPath(el);
  }

  function visible(el) {
    if (el.hidden || el.closest("[hidden]")) return false;
    var h = hostOf(el);
    if (h && !visible(h)) return false;
    var st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    return el.getClientRects().length > 0;
  }

  function labelOf(el) {
    var root = el.getRootNode ? el.getRootNode() : document;
    if (el.id && root.querySelector) {
      var l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l.textContent.trim();
    }
    var p = el.closest("label");
    return p ? p.textContent.trim() : null;
  }

  var INTERACTIVE = "input,select,textarea,button,a[href],[contenteditable=''],[contenteditable=true],[role=button]";
  var OPAQUE = "canvas,embed,object";

  function tracked() {
    var set = new Set(queryAll(INTERACTIVE));
    queryAll("[id]").forEach(function (e) {
      if (e.getRootNode() !== document || e.closest("body")) set.add(e);
    });
    return Array.prototype.filter.call(Array.from(set), function (e) {
      return e.tagName !== "SCRIPT" && e.tagName !== "STYLE" && e.tagName !== "TEMPLATE";
    });
  }

  function stateOf(el) {
    var s = { key: keyOf(el), tag: el.tagName.toLowerCase(), visible: visible(el),
              enabled: !el.disabled };
    if ("value" in el && el.tagName !== "BUTTON" && el.tagName !== "LI") s.value = el.value;
    if (el.tagName === "INPUT" && /^(checkbox|radio)$/.test(el.type)) s.checked = el.checked;
    if (el.tagName === "SELECT") {
      s.options = Array.prototype.map.call(el.options, function (o) { return [o.value, o.text]; });
    }
    if (!("value" in el) || el.tagName === "BUTTON") {
      var t = (el.innerText || el.textContent || "").trim();
      if (t.length <= 200) s.text = t;
    }
    return s;
  }

  function states() { return tracked().map(stateOf); }

  function elements() {
    return queryAll(INTERACTIVE).map(function (el) {
      var editable = /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable;
      if (el.tagName === "INPUT" && /^(button|submit|hidden|reset)$/.test(el.type)) editable = false;
      var t = (el.innerText || "").trim();
      return { tag: el.tagName.toLowerCase(), id: el.id || null, name: el.getAttribute("name"),
               label: labelOf(el), css: cssOf(el), aria: el.getAttribute("aria-label"),
               text: t && t.length <= 120 ? t : null, editable: editable,
               type: el.getAttribute("type"), visible: visible(el), key: keyOf(el),
               shadow: hostOf(el) ? true : undefined };
    });
  }

  // canvas / embedded PDF or plugin objects: no HTML to read, only pixels
  function opaque() {
    return queryAll(OPAQUE).filter(visible).map(function (el) {
      var r = el.getBoundingClientRect();
      return { key: keyOf(el), tag: el.tagName.toLowerCase(),
               type: el.getAttribute("type") || null,
               rect: { x: r.left + window.scrollX, y: r.top + window.scrollY,
                       width: r.width, height: r.height } };
    });
  }

  function targetOf(el) {
    var t = (el.innerText || "").trim();
    return { key: keyOf(el), tag: el.tagName.toLowerCase(), id: el.id || null,
             name: el.getAttribute("name"), css: cssOf(el), text: t.length <= 120 ? t : null,
             type: el.getAttribute("type"), shadow: hostOf(el) ? keyOf(hostOf(el)) : null };
  }

  // The document as HTML with open shadow roots as declarative shadow DOM (Chrome 125+:
  // Element.getHTML); older browsers fall back to outerHTML, which leaves shadow content out.
  function serialize() {
    var de = document.documentElement;
    if (!de.getHTML) return de.outerHTML;
    var shadowRoots = roots().slice(1);
    var attrs = Array.prototype.map.call(de.attributes, function (a) {
      return " " + a.name + '="' + a.value.replace(/&/g, "&amp;").replace(/"/g, "&quot;") + '"';
    }).join("");
    return "<html" + attrs + ">" + de.getHTML({ shadowRoots: shadowRoots }) + "</html>";
  }

  function snapshotPayload(reason) {
    observeRoots();
    return { kind: "settled", reason: reason, ts: Date.now(), url: location.href,
             title: document.title, html: serialize(), elements: elements(), after: states(),
             opaque: opaque(), top: window.top === window };
  }
  // the recorder asks the top document for a fresh snapshot when a frame settles
  window.__echoSnapshot = function (reason) { return JSON.stringify(snapshotPayload(reason || "frame")); };

  function settle(force) {
    if (!cur) return;
    var a = cur;
    cur = null;
    var p = snapshotPayload("action");
    p.action_id = a.id;
    p.event = a.event;
    p.value = ("value" in a.el) ? a.el.value : null;
    if (a.el.tagName === "INPUT" && /^(checkbox|radio)$/.test(a.el.type)) p.value = a.el.checked ? "true" : "false";
    p.typed = a.typed;
    p.forced = !!force;
    lastSettled = p.after;
    emit(p);
  }

  // the element acted on: the first interactive (or opaque) element on the composed path, so a
  // click inside a shadow root is attributed to the inner element, not to its host
  function interesting(ev) {
    var path = ev.composedPath ? ev.composedPath() : [ev.target];
    for (var i = 0; i < path.length; i++) {
      var n = path[i];
      if (!n || n.nodeType !== 1) continue;
      if (n.matches(INTERACTIVE)) return n;
      if (ev.type === "click" && n.matches(OPAQUE)) return n;
    }
    return null;
  }

  function isToggle(el) { return el.tagName === "INPUT" && /^(checkbox|radio)$/.test(el.type); }

  function onEvent(ev) {
    var el = interesting(ev);
    if (!el) return;
    var type = ev.type;
    // On text fields `change` only commits on blur what the input events already reported.
    if (type === "change" && (el.tagName === "TEXTAREA" ||
        (el.tagName === "INPUT" && !/^(checkbox|radio|file|range|color|date)$/.test(el.type)))) return;
    if (cur && cur.el === el && (type === "input" || type === "change")) {
      if (type === "change" || cur.event === "click") cur.event = type;
      if (isToggle(el)) cur.typed = el.checked ? "true" : "false";
      else if (type === "input") {
        if (ev.inputType === "insertText" && ev.data != null) cur.typed += ev.data;
        else if (ev.inputType && ev.inputType.indexOf("delete") === 0) cur.typed = cur.typed.slice(0, -1);
        else cur.typed = el.value;
      } else if (el.tagName === "SELECT") cur.typed = el.value;
      cur.last = Date.now();
      return;
    }
    if (cur) settle(true);
    var typed = "";
    if (type === "input") typed = (ev.inputType === "insertText" && ev.data != null) ? ev.data : el.value;
    if (type === "change" && el.tagName === "SELECT") typed = el.value;
    if (isToggle(el) && type !== "click") typed = el.checked ? "true" : "false";
    cur = { id: (++seq) + "-" + Date.now(), el: el, event: type, typed: typed, start: Date.now(),
            last: Date.now() };
    var msg = { kind: "action", action_id: cur.id, event: type, ts: cur.start, url: location.href,
                target: targetOf(el), before: lastSettled || states() };
    if (el.matches(OPAQUE)) {  // a click on pixels: where, relative to the element
      var r = el.getBoundingClientRect();
      msg.point = { x: Math.round(ev.clientX - r.left), y: Math.round(ev.clientY - r.top) };
      msg.opaque = true;
    }
    emit(msg);
  }

  ["click", "input", "change"].forEach(function (t) { document.addEventListener(t, onEvent, true); });
  // A navigation started by the action: its "after" is the next document's load snapshot, which
  // the recorder pairs with the still-open action. Never settle on the old page.
  var navigating = false;
  if (window.navigation) {
    window.navigation.addEventListener("navigate", function () { navigating = true; });
  }
  window.addEventListener("pagehide", function () { navigating = true; });

  var mo = new MutationObserver(function () { lastMutation = Date.now(); });
  var MO_OPTS = { subtree: true, childList: true, attributes: true, characterData: true };
  // mutations inside shadow roots are not reported to the document's observer
  function observeRoots() {
    roots().forEach(function (r) {
      if (observed.has(r)) return;
      observed.add(r);
      mo.observe(r === document ? document.documentElement : r, MO_OPTS);
      // `change` is not a composed event: it never reaches the document from a shadow root
      if (r !== document) r.addEventListener("change", onEvent, true);
    });
  }

  // the first snapshot of a page waits for its iframes, so the screen includes their documents
  var loadedFrames = new WeakSet();
  document.addEventListener("load", function (e) {
    if (e.target && /^(IFRAME|FRAME)$/.test(e.target.tagName || "")) loadedFrames.add(e.target);
  }, true);
  function framesLoading() {
    return queryAll("iframe,frame").some(function (f) {
      var d = null;
      try { d = f.contentDocument; } catch (e) { d = null; }
      if (d) return d.readyState !== "complete" || !(f.contentWindow && f.contentWindow.__echoInstalled);
      return !loadedFrames.has(f);  // cross-origin: its load event is all this document sees
    });
  }

  function start() {
    observeRoots();
    var loadStart = Date.now(), lastScan = 0;
    setInterval(function () {
      var now = Date.now();
      if (now - lastScan > 500) { lastScan = now; observeRoots(); }
      var quiet = now - lastMutation >= QUIET_MS && pending === 0;
      if (!loadSettled) {
        if ((quiet && !framesLoading()) || now - loadStart > MAX_MS) {
          loadSettled = true;
          var p = snapshotPayload("load");
          lastSettled = p.after;
          emit(p);
        }
        return;
      }
      if (navigating) return;
      if (cur && (quiet && now - cur.last >= QUIET_MS || now - cur.start > MAX_MS)) settle(false);
    }, 50);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
