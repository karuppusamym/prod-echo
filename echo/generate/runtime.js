// Prod Echo behavior runtime (I-21a). One generic script shipped with every generated app.
// It interprets behaviors.json for the current screen; there is no app-specific code here.
//  - Inputs are strict: a field's value counts only when it arrives through input/change events,
//    so an agent that assigns .value directly fails here as it would on a framework input.
//  - Delays are replayed from the catalog.
//  - Any interaction with an element, or under a condition, that has no evidence in the catalog
//    is logged to unobserved.jsonl; the replica does nothing else for it.
(function () {
  "use strict";
  var node = document.getElementById("echo-behaviors");
  if (!node) return;
  var B = JSON.parse(node.textContent);
  var committed = new Map();
  var blockSubmit = false;

  // Same key algorithm as the capture page script, so rules address the same elements.
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
  var INTERACTIVE = "input,select,textarea,button,a[href],[contenteditable=''],[contenteditable=true],[role=button]";

  // hidden inputs carry server-rendered values; they count as committed
  document.querySelectorAll("input[type=hidden]").forEach(function (h) { committed.set(h, h.value); });

  function unobserved(el, ev, value, reason) {
    try {
      fetch("/echo-unobserved", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ screen: B.screen, element: el ? keyOf(el) : null, event: ev,
                               value: value, reason: reason, url: location.href }) });
    } catch (e) { /* ignore */ }
  }

  function q(key) { try { return document.querySelector(key); } catch (e) { return null; } }

  function num(v) { var n = parseFloat(String(v).replace(/[^0-9.\-]/g, "")); return isNaN(n) ? null : n; }

  // returns true / false, or null when the value falls where there is no evidence
  function cond(c, value) {
    if (!c) return true;
    if (c.field) {  // a condition on another field's committed state (e.g. required field)
      var f = q(c.field);
      var v = f && committed.has(f) ? String(committed.get(f)).trim() : "";
      if (c.op === "empty") return v === "";
      if (c.op === "not_empty") return v !== "";
      return null;
    }
    if (c.op === "eq") return String(value) === String(c.value);
    if (c.op === "in") return c.value.indexOf(value) >= 0 ? true : null;
    var n = num(value);
    if (n === null) return null;
    if (c.bound && n > c.bound[0] && n < c.bound[1]) return null;
    if (c.op === "gt") return n > c.value;
    if (c.op === "le") return n <= c.value;
    return null;
  }

  function setVisible(el, show) {
    if (show) { el.hidden = false; if (el.style.display === "none") el.style.display = ""; }
    else el.hidden = true;
  }

  function applyRule(r) {
    var t = r.target ? q(r.target) : null;
    if (!t) return;
    if (r.params && r.params.blocks_submit) blockSubmit = true;
    if (r.kind === "show") {
      if (r.params && r.params.text && !t.children.length) t.textContent = r.params.text;
      setVisible(t, true);
    }
    else if (r.kind === "hide") setVisible(t, false);
    else if (r.kind === "enable") t.disabled = false;
    else if (r.kind === "disable") t.disabled = true;
    else if (r.kind === "set_value") { t.value = r.params.value; committed.set(t, t.value); }
    else if (r.kind === "set_options") {
      t.innerHTML = "";
      r.params.options.forEach(function (o) {
        var opt = document.createElement("option"); opt.value = o[0]; opt.textContent = o[1];
        t.appendChild(opt);
      });
      committed.set(t, t.value);
    }
  }

  function format(el, pattern) {
    var digits = el.value.replace(/\D/g, ""), out = "", i = 0;
    for (var p = 0; p < pattern.length && i < digits.length; p++) {
      if (pattern[p] === "#") out += digits[i++];
      else out += pattern[p];
    }
    el.value = out;
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function handle(el, ev) {
    var key = keyOf(el);
    var value = ("value" in el && el.tagName !== "BUTTON") ? el.value : null;
    var seen = B.observed.some(function (o) { return o.field === key && o.event === ev; });
    if (!seen) { unobserved(el, ev, value, "element_or_event_not_observed"); return; }
    var rules = B.rules.filter(function (r) { return r.trigger.field === key && r.trigger.event === ev; });
    rules.filter(function (r) { return r.kind === "format"; }).forEach(function (r) {
      format(el, r.params.pattern); committed.set(el, el.value); value = el.value;
    });
    var delay = 0;
    rules.filter(function (r) { return r.kind === "delay"; }).forEach(function (r) { delay = Math.max(delay, r.params.ms || 0); });
    var effects = rules.filter(function (r) { return ["format", "delay", "navigate"].indexOf(r.kind) < 0; });
    // conditional rules per target: apply those whose condition holds; no evidence -> log
    var byTarget = {};
    effects.forEach(function (r) { (byTarget[r.target + "|" + r.kind.replace(/^(show|hide)$/, "vis").replace(/^(enable|disable)$/, "en")] =
      byTarget[r.target + "|" + r.kind.replace(/^(show|hide)$/, "vis").replace(/^(enable|disable)$/, "en")] || []).push(r); });
    var toApply = [], unknown = false;
    Object.keys(byTarget).forEach(function (k) {
      var group = byTarget[k], hit = null, undecided = false;
      group.forEach(function (r) {
        var c = cond(r.trigger.condition, value);
        if (c === null) undecided = true; else if (c && !hit) hit = r;
      });
      if (hit) toApply.push(hit);
      // A field-state condition that does not hold is evidence too: the other outcome (e.g. the
      // submit navigating) was observed. Value conditions with no match have no evidence.
      else if (undecided || group.every(function (r) {
        return r.trigger.condition && !r.trigger.condition.field;
      })) unknown = true;
    });
    if (unknown) unobserved(el, ev, value, "condition_not_observed");
    var blocks = (B.blocks || []).filter(function (b) { return b.trigger.field === key && b.trigger.event === ev; });
    var started = Date.now();
    var loads = blocks.map(function (b) {
      var src = q(b.param.from);
      var v = src ? (committed.has(src) ? committed.get(src) : "") : "";
      var url = b.endpoint ? b.endpoint + "?" + encodeURIComponent(b.param.name) + "=" + encodeURIComponent(v)
        : "/echo-block/" + encodeURIComponent(B.screen) + "/" + encodeURIComponent(b.id) + "?v=" + encodeURIComponent(v);
      return fetch(url).then(function (r) { return r.text(); })
        .then(function (h) { return { b: b, html: h }; });
    });
    if (!delay && !loads.length) { toApply.forEach(applyRule); return; }
    Promise.all(loads).then(function (res) {
      var wait = Math.max(0, delay - (Date.now() - started));
      return sleep(wait).then(function () {
        res.forEach(function (x) { var t = q(x.b.selector); if (t) t.innerHTML = x.html; });
        toApply.forEach(applyRule);
      });
    });
  }

  document.addEventListener("input", function (e) {
    var el = e.target.closest ? e.target.closest(INTERACTIVE) : null;
    if (!el || el.tagName === "SELECT") return;  // selects report through change
    committed.set(el, el.value);
    handle(el, "input");
  });
  document.addEventListener("change", function (e) {
    var el = e.target.closest ? e.target.closest(INTERACTIVE) : null;
    if (!el) return;
    if (el.tagName !== "SELECT" && !/^(checkbox|radio|file|range|color|date)$/.test(el.type || "")) return;
    committed.set(el, el.value);
    handle(el, "change");
  });
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest(INTERACTIVE) : null;
    if (!el) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && !/^(submit|button|checkbox|radio)$/.test(el.type || "")) return;
    handle(el, "click");
  });

  function values(form) {
    var out = {};
    Array.prototype.forEach.call(form.elements, function (f) {
      if (!f.name || f.disabled) return;
      out[f.name] = committed.has(f) ? committed.get(f) : "";
    });
    return out;
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    var sel = form.id ? "#" + form.id : "form";
    var spec = (B.forms || []).filter(function (f) { return f.selector === sel; })[0];
    e.preventDefault();
    // A rule observed to stop this submit (client-side validation seen in probing) fired.
    if (blockSubmit) { blockSubmit = false; return; }
    if (!spec) { unobserved(form, "submit", null, "form_not_observed"); return; }
    var vals = values(form);
    if (spec.method === "get") {
      var qs = Object.keys(vals).map(function (k) {
        return encodeURIComponent(k) + "=" + encodeURIComponent(vals[k]).replace(/%20/g, "+");
      }).join("&");
      location.assign(spec.action + (qs ? "?" + qs : ""));
      return;
    }
    if (!spec.writes) { unobserved(form, "submit", null, "no_write_wiring"); return; }
    fetch(spec.action || ("/echo-write/" + encodeURIComponent(B.screen)), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ form: sel, values: vals })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (x) {
      if (x.ok && x.j.redirect) { location.assign(x.j.redirect); return; }
      var err = document.getElementById("formError");
      if (err) { err.textContent = (x.j && x.j.detail) || "Rejected"; err.hidden = false; }
      document.documentElement.setAttribute("data-echo-rejected", "1");
    });
  }, true);
})();
