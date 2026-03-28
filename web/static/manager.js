/**
 * Manager UI — shared JS utilities.
 */

/**
 * Unix epoch seconds → localized date/time in the browser's timezone.
 */
function formatLocalDateTime(epoch) {
  if (epoch == null || epoch === "") return "—";
  const n = Number(epoch);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const d = new Date(n * 1000);
  return d.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

/** Results page style: M/D/YYYY, h:MM AM/PM TZ (slashes in date; en-US for consistent order). */
function formatLocalDateTimeSlash(epoch) {
  if (epoch == null || epoch === "") return "—";
  const n = Number(epoch);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const d = new Date(n * 1000);
  return d.toLocaleString(undefined, {
    month: "numeric",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

/** @deprecated Use formatLocalDateTime */
function formatTimestamp(epoch) {
  return formatLocalDateTime(epoch);
}

/**
 * Fill elements marked with `data-local-ts` (epoch seconds) with localized text.
 * @param {ParentNode} [root=document]
 */
function initLocalTimeElements(root) {
  const scope = root || document;
  const nodes = scope.querySelectorAll("[data-local-ts]");
  for (let i = 0; i < nodes.length; i += 1) {
    const el = nodes[i];
    try {
      const raw = el.getAttribute("data-local-ts");
      const variant = el.dataset.localTsFormat;
      const fmt = variant === "slash" ? formatLocalDateTimeSlash : formatLocalDateTime;
      el.textContent = fmt(raw);
    } catch (_) {
      /* keep server-rendered fallback inside the node */
    }
  }
}

function bootLocalTimes() {
  initLocalTimeElements(document);
}

const SIDEBAR_COLLAPSED_KEY = "manager-sidebar-collapsed";

function initSidebarCollapse() {
  const nav = document.getElementById("manager-sidebar");
  if (!nav) return;
  const btn = nav.querySelector(".sidebar-toggle");
  if (!btn || btn.dataset.sidebarBound === "1") return;
  btn.dataset.sidebarBound = "1";

  const titleExpanded = btn.getAttribute("data-title-expanded") || "Collapse sidebar";
  const titleCollapsed = btn.getAttribute("data-title-collapsed") || "Expand sidebar";

  function applyCollapsed(collapsed, persist) {
    const doc = document.documentElement;
    const bod = document.body;
    if (collapsed) {
      doc.classList.add("sidebar-collapsed");
      if (bod) bod.classList.add("sidebar-collapsed");
      nav.classList.add("sidebar--collapsed");
    } else {
      doc.classList.remove("sidebar-collapsed");
      if (bod) bod.classList.remove("sidebar-collapsed");
      nav.classList.remove("sidebar--collapsed");
    }
    const expanded = !collapsed;
    btn.setAttribute("aria-expanded", String(expanded));
    const t = collapsed ? titleCollapsed : titleExpanded;
    btn.title = t;
    btn.setAttribute("aria-label", t);
    nav.querySelectorAll("a[data-sidebar-label]").forEach((a) => {
      a.title = collapsed ? a.getAttribute("data-sidebar-label") || "" : "";
    });
    if (persist) {
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
      } catch (_) {}
    }
  }

  let startCollapsed = false;
  try {
    startCollapsed = localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
  } catch (_) {}
  applyCollapsed(startCollapsed, false);

  btn.addEventListener("click", (e) => {
    e.preventDefault();
    const isCollapsed = document.documentElement.classList.contains("sidebar-collapsed");
    applyCollapsed(!isCollapsed, true);
  });
}

function bootManagerShell() {
  bootLocalTimes();
  initSidebarCollapse();
}

/* _base.html loads this at end of <body>; #manager-sidebar already exists.
   Do not wait only for DOMContentLoaded — in some environments that event never fires,
   which would leave the sidebar without a click handler. */
bootManagerShell();
document.addEventListener("DOMContentLoaded", () => initLocalTimeElements(document));
window.addEventListener("load", () => initLocalTimeElements(document));
