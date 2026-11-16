// Core banking harness client code. This is the "real app" JavaScript that the generated
// replica must NOT reuse (ADR-008); its behaviors are re-inferred from captures instead.
(function () {
  "use strict";
  var SUB = {
    AUTO: [["NEW", "New vehicle"], ["USED", "Used vehicle"]],
    HOME: [["REPAIR", "Repair"], ["ADDITION", "Addition"], ["SOLAR", "Solar"]],
    PERSONAL: [["DEBT", "Debt consolidation"], ["TRAVEL", "Travel"], ["OTHER", "Other"]]
  };
  var THRESHOLD = 25000;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function renderDetails(d) {
    var h = '<h2>Customer</h2><dl class="card">' +
      '<dt>First name</dt><dd id="dFirst">' + esc(d.FIRST_NM) + '</dd>' +
      '<dt>Last name</dt><dd id="dLast">' + esc(d.LAST_NM) + '</dd>' +
      '<dt>SSN</dt><dd id="dSsn">' + esc(d.SSN) + '</dd>' +
      '<dt>Date of birth</dt><dd id="dDob">' + esc(d.BIRTH_DT) + '</dd>' +
      '<dt>Email</dt><dd id="dEmail">' + esc(d.EMAIL) + '</dd></dl>' +
      '<h3>Addresses</h3><table id="addresses"><tbody>';
    d.addresses.forEach(function (a) {
      h += '<tr class="addrRow"><td class="atype">' + esc(a.ADDR_TYPE) + '</td><td class="line1">' +
        esc(a.LINE1) + '</td><td class="city">' + esc(a.CITY) + '</td><td class="state">' +
        esc(a.STATE) + '</td><td class="zip">' + esc(a.ZIP) + '</td></tr>';
    });
    h += '</tbody></table><h3>Phones</h3><table id="phones"><tbody>';
    d.phones.forEach(function (p) {
      h += '<tr class="phoneRow"><td class="ptype">' + esc(p.PHONE_TYPE) + '</td><td class="pnum">' +
        esc(p.NUMBER) + '</td></tr>';
    });
    h += '</tbody></table><h3>Accounts</h3><table id="accts"><tbody>';
    d.accounts.forEach(function (a) {
      h += '<tr class="acctRow"><td class="acct">' + esc(a.ACCT_NO) + '</td><td class="atype">' +
        esc(a.ACCT_TYPE) + '</td></tr>';
    });
    h += '</tbody></table><a id="newLoan" href="/loan/new?acct=' + encodeURIComponent(d.acct) +
      '">New loan</a>';
    return h;
  }

  function initSearch() {
    var btn = document.getElementById("searchBtn");
    if (!btn || !document.getElementById("details")) return;
    btn.addEventListener("click", function () {
      var acct = document.getElementById("acct").value.trim();
      var details = document.getElementById("details");
      var spinner = document.getElementById("spinner");
      details.hidden = true;
      spinner.hidden = false;
      fetch("/api/customer?acct=" + encodeURIComponent(acct)).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (d) {
        spinner.hidden = true;
        if (!d) { details.innerHTML = '<p id="notFound">No customer</p>'; details.hidden = false; return; }
        details.innerHTML = renderDetails(d);
        details.hidden = false;
      });
    });
  }

  function initLoan() {
    var form = document.getElementById("loanForm");
    if (!form) return;
    var purpose = document.getElementById("purpose");
    var sub = document.getElementById("subPurpose");
    var amt = document.getElementById("loanAmt");
    var row = document.getElementById("cosignerRow");
    var ssn = document.getElementById("ssn");
    purpose.addEventListener("change", function () {
      var opts = SUB[purpose.value] || [];
      sub.innerHTML = '<option value="">-- choose --</option>' + opts.map(function (o) {
        return '<option value="' + o[0] + '">' + esc(o[1]) + '</option>';
      }).join("");
      sub.disabled = opts.length === 0;
    });
    amt.addEventListener("input", function () {
      var v = parseFloat(amt.value.replace(/[^0-9.]/g, ""));
      row.hidden = !(v > THRESHOLD);
    });
    // Client-side validation the bot never triggers (it always types an amount): only
    // behavior probing (I-22b) can observe it.
    var err = document.getElementById("formError");
    form.addEventListener("submit", function (e) {
      if (!amt.value.trim()) {
        e.preventDefault();
        err.textContent = "Loan amount is required";
        err.hidden = false;
      }
    });
    ssn.addEventListener("input", function () {
      var d = ssn.value.replace(/\D/g, "").slice(0, 9);
      var out = d.slice(0, 3);
      if (d.length > 3) out += "-" + d.slice(3, 5);
      if (d.length > 5) out += "-" + d.slice(5);
      ssn.value = out;
    });
  }

  initSearch();
  initLoan();
})();
