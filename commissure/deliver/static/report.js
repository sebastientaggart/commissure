/* Sort and filter for static pages. The table is complete without this file. */
(function () {
  "use strict";

  function cellText(row, index) {
    var cell = row.cells[index];
    return cell ? cell.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function compare(a, b, type) {
    if (type === "number") {
      var na = parseFloat(a.replace(/,/g, ""));
      var nb = parseFloat(b.replace(/,/g, ""));
      var aBad = Number.isNaN(na);
      var bBad = Number.isNaN(nb);
      if (aBad || bBad) {
        if (aBad && bBad) {
          return a.localeCompare(b);
        }
        return aBad ? 1 : -1;
      }
      return na - nb;
    }
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }

  function sortTable(table, index, descending) {
    var body = table.tBodies[0];
    var head = table.tHead && table.tHead.rows[0];
    if (!body || !head) {
      return;
    }
    var type =
      head.cells[index].getAttribute("data-type") ||
      head.cells[index].getAttribute("data-sort") ||
      "text";
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (left, right) {
      var result = compare(cellText(left, index), cellText(right, index), type);
      return descending ? -result : result;
    });
    rows.forEach(function (row) {
      body.appendChild(row);
    });
    Array.prototype.forEach.call(head.cells, function (th, i) {
      if (i === index) {
        th.setAttribute("aria-sort", descending ? "descending" : "ascending");
      } else {
        th.setAttribute("aria-sort", "none");
      }
    });
  }

  function bindRoot(root) {
    if (!root || !root.querySelectorAll) {
      return;
    }
    root.querySelectorAll("table[data-sortable]").forEach(function (table) {
      if (table.hasAttribute("data-bound")) {
        return;
      }
      table.setAttribute("data-bound", "true");
      var head = table.tHead && table.tHead.rows[0];
      if (!head) {
        return;
      }
      Array.prototype.forEach.call(head.cells, function (th, index) {
        th.tabIndex = 0;
        th.setAttribute("aria-sort", "none");
        function activate() {
          var descending = th.getAttribute("aria-sort") === "ascending";
          sortTable(table, index, descending);
        }
        th.addEventListener("click", activate);
        th.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            activate();
          }
        });
      });
    });

    root.querySelectorAll("input[data-filter]").forEach(function (input) {
      if (input.hasAttribute("data-bound")) {
        return;
      }
      input.setAttribute("data-bound", "true");
      var selector = input.getAttribute("data-filter");
      var table = selector ? document.querySelector(selector) : null;
      if (!table || !table.tBodies[0]) {
        return;
      }
      input.addEventListener("input", function () {
        var query = input.value.trim().toLowerCase();
        Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
          var haystack = row.textContent.toLowerCase();
          row.hidden = query !== "" && haystack.indexOf(query) === -1;
        });
      });
    });

    root
      .querySelectorAll('a[href^="http://"], a[href^="https://"], a[href^="//"]')
      .forEach(function (link) {
        if (!link.hasAttribute("target")) {
          link.setAttribute("target", "_blank");
        }
        if ((link.getAttribute("target") || "").toLowerCase() === "_blank") {
          var relParts = (link.getAttribute("rel") || "")
            .trim()
            .split(/\s+/)
            .filter(Boolean);
          ["noopener", "noreferrer"].forEach(function (tok) {
            if (relParts.indexOf(tok) === -1) {
              relParts.push(tok);
            }
          });
          link.setAttribute("rel", relParts.join(" "));
        }
      });
  }

  var busyDetailsWasOpen = false;

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail && event.detail.target;
    if (target && target.id === "workspace-busy-bar") {
      var d = document.getElementById("busy-status-details");
      busyDetailsWasOpen = Boolean(d && d.open);
    } else {
      busyDetailsWasOpen = false;
    }
  });

  function syncInterfaceState() {
    var root = document.getElementById("interface-root");
    if (!root) {
      return;
    }
    var bar = document.getElementById("workspace-busy-bar");
    var stateEl = root.querySelector(".interface-state");
    var titleEl = bar || stateEl;
    if (titleEl && titleEl.hasAttribute("data-page-title")) {
      var nextTitle = titleEl.getAttribute("data-page-title");
      if (nextTitle && document.title !== nextTitle) {
        document.title = nextTitle;
      }
    }
    if (busyDetailsWasOpen) {
      var busyDetails = document.getElementById("busy-status-details");
      if (busyDetails) {
        busyDetails.open = true;
      }
    }
    var isBusy = stateEl && stateEl.getAttribute("data-busy") === "true";
    var controls = root.querySelectorAll(
      'form button, form input:not([type="hidden"]), form select, form textarea'
    );
    controls.forEach(function (ctrl) {
      if (isBusy) {
        if (!ctrl.disabled) {
          ctrl.disabled = true;
          ctrl.setAttribute("data-busy-disabled", "true");
        }
      } else if (ctrl.hasAttribute("data-busy-disabled")) {
        ctrl.disabled = false;
        ctrl.removeAttribute("data-busy-disabled");
      }
    });
  }

  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.header-disclosure[open]").forEach(function (d) {
      if (!d.contains(event.target)) {
        d.open = false;
      }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    document.querySelectorAll("details.header-disclosure[open]").forEach(function (d) {
      var hadFocus = d.contains(document.activeElement);
      d.open = false;
      if (hadFocus) {
        var sum = d.querySelector("summary");
        if (sum) {
          sum.focus();
        }
      }
    });
  });

  bindRoot(document);
  syncInterfaceState();
  document.addEventListener("htmx:load", function (event) {
    bindRoot(event.detail && event.detail.elt ? event.detail.elt : document);
    syncInterfaceState();
  });
  document.addEventListener("htmx:afterSettle", syncInterfaceState);

  var supportsUserInvalid =
    typeof CSS !== "undefined" &&
    typeof CSS.supports === "function" &&
    CSS.supports("selector(:user-invalid)");
  var dirtyInputs = new WeakMap();

  function syncValidationAria(el) {
    if (!el || typeof el.checkValidity !== "function" || el.tagName !== "INPUT") {
      return;
    }
    var invalid = supportsUserInvalid
      ? el.matches(":user-invalid")
      : el.classList.contains("user-invalid-fallback");
    if (invalid) {
      el.setAttribute("aria-invalid", "true");
    } else if (el.hasAttribute("aria-invalid")) {
      el.removeAttribute("aria-invalid");
    }
  }

  document.addEventListener(
    "blur",
    function (event) {
      var el = event.target;
      if (!el || el.tagName !== "INPUT" || typeof el.checkValidity !== "function") {
        return;
      }
      if (!supportsUserInvalid) {
        var state = dirtyInputs.get(el) || { interacted: false, blurred: false };
        state.blurred = true;
        dirtyInputs.set(el, state);
        if (state.interacted) {
          el.classList.toggle("user-invalid-fallback", !el.checkValidity());
        }
      }
      syncValidationAria(el);
    },
    true
  );

  document.addEventListener("input", function (event) {
    var el = event.target;
    if (!el || el.tagName !== "INPUT" || typeof el.checkValidity !== "function") {
      return;
    }
    if (!supportsUserInvalid) {
      var state = dirtyInputs.get(el) || { interacted: false, blurred: false };
      state.interacted = true;
      dirtyInputs.set(el, state);
      if (state.blurred) {
        el.classList.toggle("user-invalid-fallback", !el.checkValidity());
      }
    }
    if (el.hasAttribute("aria-invalid")) {
      syncValidationAria(el);
    }
  });
})();
