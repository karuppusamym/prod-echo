// Observe-only listener injected by the CDP recorder with Page.addScriptToEvaluateOnNewDocument.
// It never changes the page: it listens to click/input/change in the capture phase, counts
// in-flight fetch/XHR so it knows when the page has settled, and reports through the
// Runtime binding __echoEmit. Element states "before" an action are the states at the last
// settled point; "after" states are taken once the page is quiet again.
(function () {
  "use strict";
  if (window.__echoInstalled) return;
  window.__echoInstalled = true;
  var QUIET_MS = 200, MAX_MS = 6000;
  var pending = 0, lastMutation = Date.now(), lastSettled = null, cur = null, seq = 0;
  var loadSettled = false;

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

  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
      var tag = el.tagName.toLowerCase(), i = 1, sib = el;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === el.tagName) i++;
      var cls = el.classList.length ? "." + Array.prototype.map.call(el.classList, CSS.escape).join(".") : "";
      parts.unshift(tag + cls + ":nth-of-type(" + i + ")");
      el = el.parentElement;
    }
    return parts.join(" > ");
  }

  function keyOf(el) {
    if (el.id) return "#" + el.id;
    if (el.name && document.getElementsByName(el.name).length === 1) return "[name=" + el.name + "]";
    return cssPath(el);
  }

  function visible(el) {
    if (el.hidden || el.closest("[hidden]")) return false;
    var st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    return el.getClientRects().length > 0;
  }

  function labelOf(el) {
    if (el.id) {
      var l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l.textContent.trim();
    }
    var p = el.closest("label");
    return p ? p.textContent.trim() : null;
  }

  var INTERACTIVE = "input,select,textarea,button,a[href],[contenteditable=''],[contenteditable=true],[role=button]";

  function tracked() {
    var set = new Set(document.querySelectorAll(INTERACTIVE));
    document.querySelectorAll("body [id]").forEach(function (e) { set.add(e); });
    return Array.prototype.filter.call(Array.from(set), function (e) {
      return e.tagName !== "SCRIPT" && e.tagName !== "STYLE";
    });
  }

  function stateOf(el) {
    var s = { key: keyOf(el), tag: el.tagName.toLowerCase(), visible: visible(el),
              enabled: !el.disabled };
    if ("value" in el && el.tagName !== "BUTTON" && el.tagName !== "LI") s.value = el.value;
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
    return Array.prototype.map.call(document.querySelectorAll(INTERACTIVE), function (el) {
      var editable = /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable;
      if (el.tagName === "INPUT" && /^(button|submit|hidden|reset)$/.test(el.type)) editable = false;
      var t = (el.innerText || "").trim();
      return { tag: el.tagName.toLowerCase(), id: el.id || null, name: el.getAttribute("name"),
               label: labelOf(el), css: cssPath(el), aria: el.getAttribute("aria-label"),
               text: t && t.length <= 120 ? t : null, editable: editable,
               type: el.getAttribute("type"), visible: visible(el) };
    });
  }

  function targetOf(el) {
    var t = (el.innerText || "").trim();
    return { key: keyOf(el), tag: el.tagName.toLowerCase(), id: el.id || null,
             name: el.getAttribute("name"), css: cssPath(el), text: t.length <= 120 ? t : null,
             type: el.getAttribute("type") };
  }

  function snapshotPayload(reason) {
    return { kind: "settled", reason: reason, ts: Date.now(), url: location.href,
             title: document.title, html: document.documentElement.outerHTML,
             elements: elements(), after: states() };
  }

  function settle(force) {
    if (!cur) return;
    var a = cur;
    cur = null;
    var p = snapshotPayload("action");
    p.action_id = a.id;
    p.event = a.event;
    p.value = ("value" in a.el) ? a.el.value : null;
    p.typed = a.typed;
    p.forced = !!force;
    lastSettled = p.after;
    emit(p);
  }

  function interesting(el) {
    return el && el.closest ? el.closest(INTERACTIVE) : null;
  }

  function onEvent(ev) {
    var el = interesting(ev.target);
    if (!el) return;
    var type = ev.type;
    // On text fields `change` only commits on blur what the input events already reported.
    if (type === "change" && (el.tagName === "TEXTAREA" ||
        (el.tagName === "INPUT" && !/^(checkbox|radio|file|range|color|date)$/.test(el.type)))) return;
    if (cur && cur.el === el && (type === "input" || type === "change")) {
      if (type === "change" || cur.event === "click") cur.event = type;
      if (type === "input") {
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
    cur = { id: (++seq) + "-" + Date.now(), el: el, event: type, typed: typed, start: Date.now(),
            last: Date.now() };
    emit({ kind: "action", action_id: cur.id, event: type, ts: cur.start, url: location.href,
           target: targetOf(el), before: lastSettled || states() });
  }

  ["click", "input", "change"].forEach(function (t) { document.addEventListener(t, onEvent, true); });
  // A navigation started by the action: its "after" is the next document's load snapshot, which
  // the recorder pairs with the still-open action. Never settle on the old page.
  var navigating = false;
  if (window.navigation) {
    window.navigation.addEventListener("navigate", function () { navigating = true; });
  }
  window.addEventListener("pagehide", function () { navigating = true; });

  function start() {
    new MutationObserver(function () { lastMutation = Date.now(); })
      .observe(document.documentElement, { subtree: true, childList: true, attributes: true,
                                           characterData: true });
    var loadStart = Date.now();
    setInterval(function () {
      var now = Date.now();
      var quiet = now - lastMutation >= QUIET_MS && pending === 0;
      if (!loadSettled) {
        if (quiet || now - loadStart > MAX_MS) {
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
