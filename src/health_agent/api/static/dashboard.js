"use strict";

/* health-agent dashboard. Bez zależności; dane wyłącznie z /dash/api. */

(function () {
  // ---------------------------------------------------------------------------
  // Stan i narzędzia
  // ---------------------------------------------------------------------------

  var state = {
    csrf: "",
    today: null,
    timezone: "Europe/Warsaw",
    days: 30,
    photoView: "",
    photos: {},
    selectedPhotos: [],
    supplements: [],
  };

  var SECTIONS = {
    przeglad: { title: "Przegląd", load: loadOverview },
    korelacje: { title: "Korelacje", load: loadCorrelations },
    zdjecia: { title: "Zdjęcia sylwetki", load: loadPhotos },
    suplementy: { title: "Suplementy", load: loadSupplements },
    przypomnienia: { title: "Przypomnienia", load: loadReminders },
    alerty: { title: "Proaktywne alerty", load: loadAlerts },
  };

  var WEEKDAYS = ["pn", "wt", "śr", "cz", "pt", "sb", "nd"];
  var WEEKDAYS_LONG = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"];

  var VIEW_LABELS = { front: "Przód", side: "Bok", back: "Tył", other: "Inne" };
  var KIND_LABELS = { text: "Tekst", supplement: "Suplement", run: "Bieg", workout: "Trening" };
  var RULE_STATUS = {
    draft: { label: "Szkic", cls: "badge-warn" },
    active: { label: "Aktywne", cls: "badge-good" },
    paused: { label: "Wstrzymane", cls: "" },
  };
  var OCCURRENCE_STATUS = {
    pending: "Oczekuje",
    retrying: "Ponawiane",
    snoozed: "Odłożone",
    queued: "W kolejce",
    delivered: "Dostarczone",
    completed: "Wykonane",
    skipped: "Pominięte",
    skipped_condition: "Warunek niespełniony",
    skipped_stale: "Brak świeżych danych",
    skipped_misfire: "Przegapione",
    cancelled: "Anulowane",
    delivery_unknown: "Niepewna wysyłka",
  };
  var OUTBOX_STATUS = {
    pending: { label: "Oczekuje", cls: "" },
    sending: { label: "Wysyłanie", cls: "" },
    sent: { label: "Wysłano", cls: "badge-good" },
    held: { label: "Wstrzymana", cls: "" },
    cancelled: { label: "Anulowano", cls: "" },
    delivery_unknown: { label: "Niepewna wysyłka", cls: "badge-warn" },
  };
  var EVALUATION_STATUS = {
    triggered: { label: "Warunek spełniony", cls: "badge-warn" },
    clear: { label: "Warunek niespełniony", cls: "badge-good" },
    insufficient: { label: "Za mało danych", cls: "" },
    cooldown: { label: "Okres ciszy", cls: "badge-accent" },
  };
  var CORRELATION_STATUS = {
    qualifying: { label: "Zauważalna zależność", cls: "badge-accent" },
    below_threshold: { label: "Słaba lub brak", cls: "" },
    insufficient_data: { label: "Za mało danych", cls: "badge-warn" },
  };
  var CONDITION_LABELS = {
    steps_below: "kroki poniżej",
    protein_below: "białko poniżej",
    no_run: "brak biegu",
    no_workout: "brak treningu",
  };
  var SPORT_LABELS = {
    Run: "Bieg",
    VirtualRun: "Bieg (bieżnia)",
    TrailRun: "Bieg w terenie",
    Walk: "Spacer",
    Hike: "Wędrówka",
    Ride: "Rower",
    VirtualRide: "Rower (trenażer)",
    Swim: "Pływanie",
    WeightTraining: "Siłownia",
    Workout: "Trening",
    Yoga: "Joga",
  };

  function $(id) { return document.getElementById(id); }

  function h(tag, props) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value == null) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "dataset") Object.keys(value).forEach(function (k) { node.dataset[k] = value[k]; });
        else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), value);
        else if (key in node && typeof value === "boolean") node[key] = value;
        else node.setAttribute(key, value);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(node, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  var SVG_NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (key) {
      if (attrs[key] != null) node.setAttribute(key, attrs[key]);
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function fmt(value, decimals) {
    if (value == null || !isFinite(value)) return "—";
    return Number(value).toLocaleString("pl-PL", {
      minimumFractionDigits: decimals || 0,
      maximumFractionDigits: decimals || 0,
    });
  }

  function fmtSigned(value, decimals) {
    if (value == null) return "—";
    var text = fmt(Math.abs(value), decimals);
    if (value > 0) return "+" + text;
    if (value < 0) return "−" + text;
    return "±" + text;
  }

  function withUnit(text, unit) {
    if (!unit) return text;
    return unit === "%" || unit === "/5" ? text + unit : text + " " + unit;
  }

  function parseDate(iso) {
    var parts = iso.split("-");
    return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  }

  function fmtDay(iso) {
    var date = parseDate(iso);
    return String(date.getDate()).padStart(2, "0") + "." + String(date.getMonth() + 1).padStart(2, "0");
  }

  function fmtDayFull(iso) {
    return parseDate(iso).toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit", year: "numeric" });
  }

  function fmtDayLong(iso) {
    return parseDate(iso).toLocaleDateString("pl-PL", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }

  function fmtDayShort(iso) {
    var date = parseDate(iso);
    return WEEKDAYS[(date.getDay() + 6) % 7] + " " + fmtDay(iso);
  }

  function fmtDateTime(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString("pl-PL", {
      day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
      timeZone: state.timezone,
    });
  }

  function fmtTime(iso) {
    return new Date(iso).toLocaleTimeString("pl-PL", {
      hour: "2-digit", minute: "2-digit", timeZone: state.timezone,
    });
  }

  function dateInAppTimezone(value) {
    var parts = new Intl.DateTimeFormat("en-CA", {
      year: "numeric", month: "2-digit", day: "2-digit", timeZone: state.timezone,
    }).formatToParts(value);
    var byType = {};
    parts.forEach(function (part) { byType[part.type] = part.value; });
    return byType.year + "-" + byType.month + "-" + byType.day;
  }

  function relativeDay(iso) {
    if (!iso) return "";
    if (iso === state.today) return "dziś";
    var diff = Math.round((parseDate(state.today) - parseDate(iso)) / 86400000);
    if (diff === 1) return "wczoraj";
    if (diff === 2) return "przedwczoraj";
    return fmtDayFull(iso);
  }

  function relativeTime(iso) {
    var minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (minutes < 1) return "przed chwilą";
    if (minutes < 60) return minutes + " min temu";
    var hours = Math.round(minutes / 60);
    if (hours < 48) return hours + " godz. temu";
    return Math.round(hours / 24) + " dni temu";
  }

  function plural(n, one, few, many) {
    var mod10 = n % 10, mod100 = n % 100;
    if (n === 1) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function badge(label, cls) {
    return h("span", { class: "badge " + (cls || "") }, label);
  }

  // ---------------------------------------------------------------------------
  // API, komunikaty, potwierdzenia
  // ---------------------------------------------------------------------------

  function ApiError(message, status) {
    this.message = message;
    this.status = status;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function detailText(body, status) {
    var detail = body && body.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map(function (item) {
        var where = (item.loc || []).slice(1).join(".");
        return (where ? where + ": " : "") + (item.msg || "");
      }).join("; ");
    }
    if (status === 413) return "Plik jest za duży.";
    if (status === 429) return "Za dużo żądań. Odczekaj chwilę.";
    return "Serwer zwrócił błąd " + status + ".";
  }

  async function api(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    var method = opts.method || "GET";
    var body = opts.body;
    if (method !== "GET") headers["X-CSRF-Token"] = state.csrf;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }
    var response;
    try {
      response = await fetch("/dash/api" + path, {
        method: method, headers: headers, body: body, credentials: "same-origin",
      });
    } catch (err) {
      throw new ApiError("Brak połączenia z serwerem. Sprawdź sieć i spróbuj ponownie.", 0);
    }
    if (response.status === 401) {
      location.replace("/dash" + location.hash);
      throw new ApiError("Sesja wygasła. Zaloguj się ponownie.", 401);
    }
    var parsed = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new ApiError(detailText(parsed, response.status), response.status);
    return parsed;
  }

  function toast(message, kind) {
    var container = $("toasts");
    var node = h("div", { class: "toast" + (kind ? " toast-" + kind : ""), role: kind === "error" ? "alert" : "status" },
      h("span", { text: message }),
      h("button", { type: "button", "aria-label": "Zamknij komunikat", onclick: function () { node.remove(); } }, "×")
    );
    container.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "error" ? 9000 : 4500);
  }

  function confirmAction(options) {
    var dialog = $("confirmDialog");
    $("confirmTitle").textContent = options.title || "Potwierdź";
    $("confirmText").textContent = options.text || "";
    var button = $("confirmButton");
    button.textContent = options.confirmLabel || "Potwierdź";
    button.className = "btn " + (options.danger === false ? "btn-primary" : "btn-danger");
    return new Promise(function (resolve) {
      dialog.addEventListener("close", function handler() {
        dialog.removeEventListener("close", handler);
        resolve(dialog.returnValue === "confirm");
      });
      // Escape zamyka bez zmiany returnValue, więc trzeba go wyzerować przed otwarciem.
      dialog.returnValue = "";
      dialog.showModal();
      // Domyślny fokus na „Anuluj”: przypadkowy Enter nie wykonuje akcji.
      dialog.querySelector('button[value="cancel"]').focus();
    });
  }

  /* fields: [{name,label,type,value,options,checked,min,max,step,required}] */
  function editDialog(title, fields) {
    var dialog = $("editDialog");
    var container = $("editFields");
    $("editTitle").textContent = title;
    clear(container);
    fields.forEach(function (field) {
      var control;
      if (field.type === "select") {
        control = h("select", { name: field.name, id: "edit-" + field.name });
        field.options.forEach(function (option) {
          control.appendChild(h("option", { value: option.value, selected: option.value === String(field.value == null ? "" : field.value) }, option.label));
        });
      } else if (field.type === "weekdays") {
        control = h("div", { class: "weekdays", role: "group", "aria-label": field.label });
        WEEKDAYS.forEach(function (day, index) {
          control.appendChild(h("label", null,
            h("input", { type: "checkbox", name: field.name, value: String(index), checked: field.value.indexOf(index) >= 0 }),
            WEEKDAYS_LONG[index]
          ));
        });
        container.appendChild(h("div", { class: "field field-wide" }, h("span", { text: field.label }), control));
        return;
      } else {
        control = h("input", {
          type: field.type || "text", name: field.name, id: "edit-" + field.name,
          value: field.value == null ? "" : String(field.value), maxlength: field.maxlength,
          min: field.min, max: field.max, step: field.step, required: !!field.required,
          inputmode: field.type === "number" ? "decimal" : null, placeholder: field.placeholder,
        });
      }
      container.appendChild(h("label", { class: "field" + (field.wide ? " field-wide" : "") }, h("span", { text: field.label }), control));
    });
    return new Promise(function (resolve) {
      var form = $("editForm");
      function onClose() {
        dialog.removeEventListener("close", onClose);
        if (dialog.returnValue !== "save") { resolve(null); return; }
        var values = {};
        fields.forEach(function (field) {
          if (field.type === "weekdays") {
            values[field.name] = Array.prototype.map.call(
              form.querySelectorAll('input[name="' + field.name + '"]:checked'),
              function (input) { return Number(input.value); }
            );
          } else {
            var raw = form.elements[field.name].value;
            values[field.name] = field.type === "number" ? (raw === "" ? null : Number(raw)) : raw;
          }
        });
        resolve(values);
      }
      dialog.addEventListener("close", onClose);
      dialog.returnValue = "";
      dialog.showModal();
      var first = container.querySelector("input, select");
      if (first) first.focus();
    });
  }

  async function withBusy(button, task) {
    if (button.disabled) return;
    button.disabled = true;
    button.classList.add("is-busy");
    try {
      await task();
    } catch (err) {
      toast(err && err.message ? err.message : "Nieznany błąd.", "error");
    } finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  function renderStatus(slotId, kind, message, retry) {
    var slot = $(slotId);
    clear(slot);
    if (kind === "loading") {
      slot.appendChild(h("div", { class: "status", role: "status", "aria-label": "Ładowanie" },
        h("span", { class: "skeleton", style: "width:60%" }),
        h("span", { class: "skeleton", style: "width:85%" }),
        h("span", { class: "skeleton", style: "width:40%" })
      ));
    } else if (kind === "error") {
      slot.appendChild(h("div", { class: "status status-error", role: "alert" },
        h("strong", { text: "Nie udało się pobrać danych. " }),
        h("span", { text: message }),
        retry ? h("div", null, h("button", { class: "btn btn-sm", type: "button", onclick: retry }, "Spróbuj ponownie")) : null
      ));
    } else if (kind === "empty") {
      slot.appendChild(h("div", { class: "status status-empty" }, message));
    }
  }

  /* Pokazuje szkielet ładowania; jeśli task sam wyrenderował stan „brak danych”
     w tym slocie, zostaje on na miejscu. Błąd zamienia slot w komunikat z retry. */
  async function loadInto(slotId, task) {
    var slot = $(slotId);
    renderStatus(slotId, "loading");
    var loader = slot.firstChild;
    try {
      await task();
      if (loader && loader.parentNode === slot) loader.remove();
    } catch (err) {
      renderStatus(slotId, "error", err && err.message ? err.message : "Nieznany błąd.", function () { loadInto(slotId, task); });
    }
  }

  // ---------------------------------------------------------------------------
  // Nawigacja
  // ---------------------------------------------------------------------------

  function currentSection() {
    var id = location.hash.replace("#", "");
    if (!id) return "przeglad";
    // Kotwice spoza listy sekcji (np. „Przejdź do treści”) nie zmieniają widoku.
    return SECTIONS[id] ? id : null;
  }

  function route() {
    var id = currentSection();
    if (!id) return;
    Object.keys(SECTIONS).forEach(function (key) {
      $(key).hidden = key !== id;
    });
    document.querySelectorAll(".nav a").forEach(function (link) {
      if (link.dataset.section === id) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.title = SECTIONS[id].title + " — health-agent";
    window.scrollTo({ top: 0, behavior: "auto" });
    SECTIONS[id].load();
  }

  // ---------------------------------------------------------------------------
  // Wykresy (SVG, bez bibliotek)
  // ---------------------------------------------------------------------------

  var CHART_W = 600, CHART_H = 200, ML = 46, MR = 14, MT = 14, MB = 24;

  function niceStep(rough) {
    var exponent = Math.pow(10, Math.floor(Math.log10(rough)));
    var fraction = rough / exponent;
    var nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
    return nice * exponent;
  }

  function scale(values, kind) {
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    var lo, hi;
    if (kind === "total") {
      lo = 0;
      hi = max > 0 ? max : 1;
    } else {
      var span = max - min;
      var pad = span > 0 ? span * 0.2 : Math.max(Math.abs(max) * 0.05, 1);
      lo = min - pad;
      hi = max + pad;
      if (min >= 0 && lo < 0) lo = 0;
    }
    var step = niceStep((hi - lo) / 4);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    if (hi === lo) hi = lo + step;
    var ticks = [];
    for (var v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(6)));
    return { lo: lo, hi: hi, ticks: ticks, tickDecimals: step % 1 === 0 ? 0 : String(step).split(".")[1].length };
  }

  function chartFigure(spec, series) {
    var points = series.map(function (row) { return { date: row.date, value: row[spec.key] }; });
    var present = points.filter(function (p) { return p.value != null; });
    var figure = h("figure", { class: "chart", "aria-label": spec.label });
    var head = h("div", { class: "chart-head" }, h("h3", { text: withUnit(spec.label, spec.unit ? "(" + spec.unit + ")" : "") }));
    figure.appendChild(head);
    if (!present.length) {
      figure.appendChild(h("div", { class: "chart-empty" }, "Brak danych w tym okresie."));
      return figure;
    }
    var values = present.map(function (p) { return p.value; });
    var minValue = Math.min.apply(null, values), maxValue = Math.max.apply(null, values);
    head.appendChild(h("span", { class: "chart-range" },
      present.length + " z " + points.length + " dni · " + fmt(minValue, spec.decimals) + "–" + fmt(maxValue, spec.decimals)
    ));

    var sc = scale(values, spec.kind);
    var n = points.length;
    var innerW = CHART_W - ML - MR, innerH = CHART_H - MT - MB;
    var band = innerW / n;
    function x(i) { return ML + (i + 0.5) * band; }
    function y(v) { return MT + innerH - ((v - sc.lo) / (sc.hi - sc.lo)) * innerH; }

    var svg = s("svg", { viewBox: "0 0 " + CHART_W + " " + CHART_H, role: "img", tabindex: "0",
      "aria-label": spec.label + ", wykres " + n + " dni. Dane w tabeli poniżej." });

    var grid = s("g", { class: "grid" });
    var axis = s("g", { class: "axis" });
    sc.ticks.forEach(function (tick) {
      grid.appendChild(s("line", { x1: ML, x2: CHART_W - MR, y1: y(tick), y2: y(tick) }));
      axis.appendChild(s("text", { x: ML - 6, y: y(tick) + 4, "text-anchor": "end" }, fmt(tick, sc.tickDecimals)));
    });
    svg.appendChild(grid);
    svg.appendChild(s("line", { class: "baseline", x1: ML, x2: CHART_W - MR, y1: MT + innerH, y2: MT + innerH }));

    var labelEvery = Math.max(1, Math.ceil(n / 6));
    for (var i = 0; i < n; i += labelEvery) {
      if (n - i < labelEvery / 2 && i !== 0) break;
      axis.appendChild(s("text", { x: x(i), y: CHART_H - 6, "text-anchor": "middle" }, fmtDay(points[i].date)));
    }
    svg.appendChild(axis);

    var marks = s("g");
    if (spec.kind === "total") {
      var barW = Math.min(24, Math.max(2, band - 2));
      points.forEach(function (p, idx) {
        if (p.value == null) return;
        var top = y(p.value), bottom = MT + innerH, height = bottom - top;
        var r = Math.min(4, barW / 2, height);
        var left = x(idx) - barW / 2;
        var d = "M" + left + "," + bottom + " V" + (top + r) + " Q" + left + "," + top + " " + (left + r) + "," + top +
          " H" + (left + barW - r) + " Q" + (left + barW) + "," + top + " " + (left + barW) + "," + (top + r) +
          " V" + bottom + " Z";
        marks.appendChild(s("path", { class: "series-bar", d: d }));
      });
    } else {
      var segments = [], current = [];
      points.forEach(function (p, idx) {
        if (p.value == null) { if (current.length) segments.push(current); current = []; return; }
        current.push({ x: x(idx), y: y(p.value) });
      });
      if (current.length) segments.push(current);
      segments.forEach(function (segment) {
        if (segment.length === 1) {
          marks.appendChild(s("circle", { class: "series-dot", cx: segment[0].x, cy: segment[0].y, r: 4 }));
          return;
        }
        var line = segment.map(function (pt, idx) { return (idx ? "L" : "M") + pt.x + "," + pt.y; }).join(" ");
        var bottom = MT + innerH;
        marks.appendChild(s("path", { class: "series-area", d: line + " L" + segment[segment.length - 1].x + "," + bottom + " L" + segment[0].x + "," + bottom + " Z" }));
        marks.appendChild(s("path", { class: "series-line", d: line }));
        if (n <= 31) segment.forEach(function (pt) {
          marks.appendChild(s("circle", { class: "series-dot", cx: pt.x, cy: pt.y, r: 4 }));
        });
      });
    }
    svg.appendChild(marks);

    // Etykieta ostatniej wartości
    var lastIndex = -1;
    for (var k = n - 1; k >= 0; k--) if (points[k].value != null) { lastIndex = k; break; }
    var lastX = x(lastIndex), lastY = y(points[lastIndex].value);
    var labelText = fmt(points[lastIndex].value, spec.decimals);
    var anchor = lastX > CHART_W - MR - 40 ? "end" : "start";
    if (spec.kind !== "total") marks.appendChild(s("circle", { class: "series-dot", cx: lastX, cy: lastY, r: 4.5 }));
    svg.appendChild(s("text", { class: "end-label", x: anchor === "end" ? lastX - 8 : lastX + 8, y: lastY - 8, "text-anchor": anchor }, labelText));

    // Warstwa hover / klawiatura
    var hoverLine = s("line", { class: "hover-line", x1: 0, x2: 0, y1: MT, y2: MT + innerH, visibility: "hidden" });
    var hoverDot = s("circle", { class: "hover-dot", r: 5, visibility: "hidden" });
    var hit = s("rect", { class: "hit", x: ML, y: MT, width: innerW, height: innerH });
    svg.appendChild(hoverLine);
    svg.appendChild(hoverDot);
    svg.appendChild(hit);
    var tip = h("div", { class: "chart-tip", hidden: true, role: "status" });
    figure.appendChild(svg);
    figure.appendChild(tip);

    var activeIndex = -1;
    function showIndex(idx) {
      if (idx < 0 || idx >= n) { hideHover(); return; }
      activeIndex = idx;
      var p = points[idx];
      var rect = svg.getBoundingClientRect();
      var px = x(idx);
      hoverLine.setAttribute("x1", px);
      hoverLine.setAttribute("x2", px);
      hoverLine.setAttribute("visibility", "visible");
      var figureRect = figure.getBoundingClientRect();
      var leftPx = rect.left - figureRect.left + (px / CHART_W) * rect.width;
      var topPx;
      if (p.value != null) {
        hoverDot.setAttribute("cx", px);
        hoverDot.setAttribute("cy", y(p.value));
        hoverDot.setAttribute("visibility", "visible");
        topPx = rect.top - figureRect.top + (y(p.value) / CHART_H) * rect.height - 10;
      } else {
        hoverDot.setAttribute("visibility", "hidden");
        topPx = rect.top - figureRect.top + (MT / CHART_H) * rect.height + 20;
      }
      tip.textContent = fmtDayShort(p.date) + " · " + (p.value == null ? "brak pomiaru" : withUnit(fmt(p.value, spec.decimals), spec.unit));
      tip.hidden = false;
      var half = tip.offsetWidth / 2;
      leftPx = Math.max(half, Math.min(figureRect.width - half, leftPx));
      tip.style.left = leftPx + "px";
      tip.style.top = topPx + "px";
    }
    function hideHover() {
      activeIndex = -1;
      hoverLine.setAttribute("visibility", "hidden");
      hoverDot.setAttribute("visibility", "hidden");
      tip.hidden = true;
    }
    function indexFromEvent(event) {
      var rect = svg.getBoundingClientRect();
      var sx = ((event.clientX - rect.left) / rect.width) * CHART_W;
      return Math.max(0, Math.min(n - 1, Math.floor((sx - ML) / band)));
    }
    hit.addEventListener("pointermove", function (event) { showIndex(indexFromEvent(event)); });
    hit.addEventListener("pointerdown", function (event) { showIndex(indexFromEvent(event)); });
    svg.addEventListener("pointerleave", hideHover);
    svg.addEventListener("keydown", function (event) {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        var next = activeIndex < 0 ? lastIndex : activeIndex + (event.key === "ArrowLeft" ? -1 : 1);
        showIndex(Math.max(0, Math.min(n - 1, next)));
      } else if (event.key === "Escape") hideHover();
    });
    svg.addEventListener("blur", hideHover);

    // Tabela jako dostępna alternatywa
    var table = h("table", null,
      h("thead", null, h("tr", null, h("th", { text: "Data" }), h("th", { class: "num", text: withUnit("Wartość", spec.unit ? "(" + spec.unit + ")" : "") }))),
      h("tbody", null, points.slice().reverse().map(function (p) {
        return h("tr", null, h("td", { text: fmtDayShort(p.date) }), h("td", { class: "num", text: p.value == null ? "brak" : fmt(p.value, spec.decimals) }));
      }))
    );
    figure.appendChild(h("details", null, h("summary", { text: "Pokaż dane jako tabelę" }), h("div", { class: "table-wrap" }, table)));
    return figure;
  }

  // ---------------------------------------------------------------------------
  // Przegląd
  // ---------------------------------------------------------------------------

  function tile(metric, days) {
    var node = h("div", { class: "tile" });
    node.appendChild(h("div", { class: "tile-label", text: metric.label }));
    if (metric.latest == null) {
      node.appendChild(h("div", { class: "tile-value is-empty", text: "brak danych" }));
      node.appendChild(h("div", { class: "tile-meta", text: "Nic w ostatnich " + days + " dniach" }));
      return node;
    }
    node.appendChild(h("div", { class: "tile-value" }, fmt(metric.latest, metric.decimals), metric.unit ? h("small", { text: metric.unit }) : null));
    var meta = [];
    meta.push(h("span", { text: relativeDay(metric.latest_date) }));
    if (metric.average != null) {
      meta.push(h("span", { text: " · śr. " + fmt(metric.average, metric.decimals) }));
    }
    node.appendChild(h("div", { class: "tile-meta" }, meta));
    if (metric.delta != null) {
      var dir = metric.delta > 0 ? "up" : metric.delta < 0 ? "down" : "flat";
      var arrow = dir === "flat" ? null : s("svg", { viewBox: "0 0 10 10", "aria-hidden": "true" },
        s("path", { d: dir === "up" ? "M1 8 L5 2 L9 8 Z" : "M1 2 L5 8 L9 2 Z", fill: "currentColor" }));
      node.appendChild(h("div", { class: "tile-meta" },
        h("span", { class: "tile-delta " + dir, title: "Zmiana średniej wobec poprzednich " + days + " dni" }, arrow, fmtSigned(metric.delta, metric.decimals)),
        " vs poprz. " + days + " dni (" + metric.days_with_data + " vs " + metric.previous_days_with_data + " dni z danymi)"
      ));
    } else {
      node.appendChild(h("div", { class: "tile-meta", text: "Brak porównywalnych danych (" +
        metric.days_with_data + " vs " + metric.previous_days_with_data + " dni)" }));
    }
    return node;
  }

  function renderOverview(data) {
    var days = data.range.days;
    $("overviewSubtitle").textContent =
      fmtDayLong(data.today) + " · " + fmtDay(data.range.start) + "–" + fmtDayFull(data.range.end) +
      " · wartości z zapisanych danych, brak pomiaru nie jest zerem.";

    var tiles = $("tiles");
    clear(tiles);
    var metrics = data.metrics.filter(function (m) { return m.key !== "wellbeing" || m.days_with_data > 0; });
    metrics.forEach(function (metric) { tiles.appendChild(tile(metric, days)); });

    var charts = $("charts");
    clear(charts);
    var anyData = metrics.some(function (m) { return m.days_with_data > 0; });
    if (!anyData) {
      renderStatus("overviewStatus", "empty",
        "W tym okresie nie ma żadnych zapisanych pomiarów. Sprawdź synchronizację Intervals.icu i webhook Health Connect.");
    }
    metrics.forEach(function (metric) { charts.appendChild(chartFigure(metric, data.series)); });

    var workouts = $("workouts");
    clear(workouts);
    if (!data.workouts.length) {
      workouts.appendChild(h("p", { class: "hint", text: "Brak treningów w tym okresie." }));
    } else {
      workouts.appendChild(h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null,
          h("th", { text: "Data" }), h("th", { text: "Rodzaj" }), h("th", { class: "num", text: "Czas" }),
          h("th", { class: "num", text: "Dystans" }), h("th", { class: "num", text: "Tętno śr." })
        )),
        h("tbody", null, data.workouts.map(function (w) {
          return h("tr", null,
            h("td", { text: fmtDayShort(w.date) + " " + fmtTime(w.started_at) }),
            h("td", { text: SPORT_LABELS[w.sport] || w.sport || "—" }),
            h("td", { class: "num", text: w.duration_min == null ? "—" : fmt(w.duration_min, 0) + " min" }),
            h("td", { class: "num", text: w.distance_km == null ? "—" : fmt(w.distance_km, 2) + " km" }),
            h("td", { class: "num", text: w.avg_hr == null ? "—" : fmt(w.avg_hr, 0) })
          );
        }))
      )));
    }

    var freshness = $("freshness");
    clear(freshness);
    if (!data.freshness.length) {
      freshness.appendChild(h("p", { class: "hint", text: "Żadne źródło nie zgłosiło jeszcze danych." }));
    } else {
      freshness.appendChild(h("table", null, h("tbody", null, data.freshness.map(function (row) {
        var ageHours = (Date.now() - new Date(row.received_at).getTime()) / 3600000;
        var status = ageHours <= 24 ? badge("aktualne", "badge-good") : ageHours > 72 ? badge("dawno", "badge-warn") : badge(">24 h", "");
        return h("tr", null,
          h("td", null, h("div", { text: row.label }), h("div", { class: "item-meta", text: row.source_label })),
          h("td", null, h("div", { text: "dane do " + fmtDayFull(row.observed_through) }),
            h("div", { class: "item-meta", text: "odebrano " + relativeTime(row.received_at) })),
          h("td", { class: "num" }, status)
        );
      }))));
    }
  }

  function loadOverview() {
    document.querySelectorAll('[data-days]').forEach(function (button) {
      button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.days));
    });
    return loadInto("overviewStatus", async function () {
      var data = await api("/overview?days=" + state.days);
      renderOverview(data);
    });
  }

  // ---------------------------------------------------------------------------
  // Korelacje
  // ---------------------------------------------------------------------------

  function renderCorrelations(items) {
    var container = $("correlations");
    clear(container);
    items.forEach(function (item) {
      var status = CORRELATION_STATUS[item.status] || { label: item.status, cls: "" };
      var node = h("div", { class: "item" });
      node.appendChild(h("div", { class: "item-head" },
        h("div", null, h("div", { class: "item-title", text: item.label.charAt(0).toUpperCase() + item.label.slice(1) }),
          h("div", { class: "item-sub", text: item.rho == null
            ? "Za mało kompletnych dni, żeby policzyć zależność (jest " + item.n + ", potrzeba 20)."
            : "rho = " + fmt(item.rho, 2) + " · " + item.n + " " + plural(item.n, "dzień", "dni", "dni") + " z obiema wartościami" })),
        badge(status.label, status.cls)
      ));
      if (item.rho != null) {
        var pct = Math.abs(item.rho) * 50;
        var fill = h("div", { class: "rho-fill", style: item.rho >= 0 ? "left:50%;width:" + pct + "%" : "right:50%;width:" + pct + "%" });
        node.appendChild(h("div", { class: "rho-meter", role: "img", "aria-label": "rho " + fmt(item.rho, 2) + " na skali od −1 do 1" }, fill));
        node.appendChild(h("div", { class: "rho-scale" }, h("span", { text: "−1" }), h("span", { text: "0" }), h("span", { text: "+1" })));
      }
      container.appendChild(node);
    });
    container.appendChild(h("p", { class: "hint",
      text: "Do wiedzy agentów trafia dopiero para z co najmniej 20 dniami i |rho| ≥ 0,4 — i tylko jako obserwacja o niskiej pewności." }));
  }

  function loadCorrelations() {
    return loadInto("correlationsStatus", async function () {
      renderCorrelations(await api("/correlations"));
    });
  }

  // ---------------------------------------------------------------------------
  // Zdjęcia
  // ---------------------------------------------------------------------------

  function photoTitle(photo) {
    return (VIEW_LABELS[photo.view] || photo.view) + " · " + fmtDayFull(photo.captured_date);
  }

  function photoMeasurement(photo) {
    if (photo.weight_kg == null && photo.fat_pct == null) return "brak pomiaru z tego dnia";
    var parts = [];
    if (photo.weight_kg != null) parts.push(fmt(photo.weight_kg, 1) + " kg");
    if (photo.fat_pct != null) parts.push(fmt(photo.fat_pct, 1) + "% tłuszczu");
    return parts.join(" · ");
  }

  function renderCompare() {
    var panel = $("compare");
    var grid = $("compareGrid");
    clear(grid);
    var selected = state.selectedPhotos.map(function (id) { return state.photos[id]; }).filter(Boolean);
    $("compareHint").textContent = selected.length === 1
      ? "Zaznacz jeszcze jedno zdjęcie, żeby je porównać."
      : "Zaznacz dwa zdjęcia, żeby je porównać.";
    if (selected.length < 2) { panel.hidden = true; return; }
    selected.sort(function (a, b) { return a.captured_date < b.captured_date ? -1 : 1; });
    selected.forEach(function (photo) {
      grid.appendChild(h("figure", null,
        h("img", { src: "/dash/api/photos/" + photo.id + "/file", alt: "Zdjęcie: " + photoTitle(photo), loading: "lazy" }),
        h("figcaption", null, h("strong", { text: photoTitle(photo) }), h("div", { text: photoMeasurement(photo) }))
      ));
    });
    var a = selected[0], b = selected[1];
    var daysBetween = Math.round((parseDate(b.captured_date) - parseDate(a.captured_date)) / 86400000);
    var parts = [daysBetween + " " + plural(daysBetween, "dzień", "dni", "dni") + " różnicy"];
    if (a.weight_kg != null && b.weight_kg != null) parts.push("waga " + fmtSigned(b.weight_kg - a.weight_kg, 1) + " kg");
    if (a.fat_pct != null && b.fat_pct != null) parts.push("tłuszcz " + fmtSigned(b.fat_pct - a.fat_pct, 1) + " pkt proc.");
    if (a.view !== b.view) parts.push("różne ujęcia");
    grid.appendChild(h("div", { class: "compare-diff", text: parts.join(" · ") }));
    panel.hidden = false;
  }

  function togglePhotoSelection(id, checked) {
    var index = state.selectedPhotos.indexOf(id);
    if (checked && index < 0) {
      state.selectedPhotos.push(id);
      if (state.selectedPhotos.length > 2) state.selectedPhotos.shift();
    } else if (!checked && index >= 0) {
      state.selectedPhotos.splice(index, 1);
    }
    document.querySelectorAll(".photo").forEach(function (card) {
      var selected = state.selectedPhotos.indexOf(Number(card.dataset.id)) >= 0;
      card.classList.toggle("is-selected", selected);
      card.querySelector('input[type="checkbox"]').checked = selected;
    });
    renderCompare();
  }

  function renderPhotos(items) {
    var container = $("photos");
    clear(container);
    items.forEach(function (photo) { state.photos[photo.id] = photo; });
    if (!items.length) {
      renderStatus("photosStatus", "empty", state.photoView
        ? "Brak zdjęć z ujęciem „" + VIEW_LABELS[state.photoView] + "”."
        : "Archiwum jest puste. Dodaj zdjęcie powyżej albo wyślij je na Telegram z podpisem, np. „przód 2026-09-19 rano”.");
    }
    items.forEach(function (photo) {
      var selected = state.selectedPhotos.indexOf(photo.id) >= 0;
      var card = h("article", { class: "photo" + (selected ? " is-selected" : ""), dataset: { id: String(photo.id) } });
      card.appendChild(h("img", { src: "/dash/api/photos/" + photo.id + "/file", alt: "Zdjęcie: " + photoTitle(photo), loading: "lazy", width: photo.width, height: photo.height }));
      var body = h("div", { class: "photo-body" });
      body.appendChild(h("div", { class: "photo-title", text: photoTitle(photo) }));
      body.appendChild(h("div", { class: "photo-meta", text: photoMeasurement(photo) }));
      if (photo.note) body.appendChild(h("div", { class: "photo-note", text: photo.note }));
      var deleteButton = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": "Usuń zdjęcie " + photoTitle(photo) }, "Usuń");
      deleteButton.addEventListener("click", function () {
        withBusy(deleteButton, async function () {
          var ok = await confirmAction({
            title: "Usunąć zdjęcie?",
            text: photoTitle(photo) + " zostanie usunięte z archiwum i z dysku. Tej operacji nie da się cofnąć.",
            confirmLabel: "Usuń zdjęcie",
          });
          if (!ok) return;
          await api("/photos/" + photo.id, { method: "DELETE" });
          delete state.photos[photo.id];
          state.selectedPhotos = state.selectedPhotos.filter(function (id) { return id !== photo.id; });
          toast("Usunięto zdjęcie.", "success");
          await loadPhotos();
        });
      });
      var checkbox = h("input", { type: "checkbox", checked: selected });
      checkbox.addEventListener("change", function () { togglePhotoSelection(photo.id, checkbox.checked); });
      body.appendChild(h("div", { class: "photo-actions" },
        h("label", { class: "photo-select" }, checkbox, "Porównaj"),
        deleteButton
      ));
      card.appendChild(body);
      container.appendChild(card);
    });
    renderCompare();
  }

  function loadPhotos() {
    document.querySelectorAll("#photoViewFilter button").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.view === state.photoView));
    });
    return loadInto("photosStatus", async function () {
      renderPhotos(await api("/photos" + (state.photoView ? "?view=" + state.photoView : "")));
    });
  }

  function setupPhotoForm() {
    var form = $("photoForm");
    var fileInput = $("photoFile");
    var preview = $("photoPreview");
    var previewImage = $("photoPreviewImage");
    var objectUrl = null;
    fileInput.addEventListener("change", function () {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = null;
      var file = fileInput.files[0];
      if (!file) { preview.hidden = true; return; }
      objectUrl = URL.createObjectURL(file);
      previewImage.src = objectUrl;
      preview.hidden = false;
      if (!$("photoDate").value) $("photoDate").value = state.today;
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = form.querySelector('button[type="submit"]');
      withBusy(button, async function () {
        var file = fileInput.files[0];
        if (!file) throw new ApiError("Wybierz plik zdjęcia.");
        if (file.size > 15 * 1024 * 1024) throw new ApiError("Zdjęcie przekracza limit 15 MB.");
        if (!$("photoDate").value) throw new ApiError("Podaj datę zdjęcia.");
        var result = await api("/photos", {
          method: "POST",
          headers: {
            "Content-Type": file.type || "application/octet-stream",
            "X-Photo-Date": $("photoDate").value,
            "X-Photo-View": $("photoView").value,
            "X-Photo-Note": encodeURIComponent($("photoNote").value.trim()),
          },
          body: file,
        });
        toast(result.deduplicated ? "To zdjęcie już było w archiwum — nic nie zduplikowano." : "Zapisano zdjęcie.", "success");
        form.reset();
        preview.hidden = true;
        $("photoDate").value = state.today;
        $("photoUploadPanel").open = false;
        await loadPhotos();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Suplementy
  // ---------------------------------------------------------------------------

  function intakeBadge(supplement) {
    if (!supplement.last_status) return badge("brak wpisów", "");
    var when = supplement.last_recorded_at ? new Date(supplement.last_recorded_at) : null;
    var todayLocal = when && dateInAppTimezone(when) === state.today;
    var label = supplement.last_status === "taken" ? "wzięte" : "pominięte";
    var cls = supplement.last_status === "taken" ? "badge-good" : "badge-warn";
    if (todayLocal) return badge("dziś " + label, cls);
    var dayText = when ? when.toLocaleDateString("pl-PL", {
      day: "2-digit", month: "2-digit", year: "numeric", timeZone: state.timezone,
    }) : "";
    return badge("ostatnio " + label + " " + dayText, "");
  }

  function supplementCard(item) {
    var card = h("div", { class: "item" + (item.active ? "" : " is-inactive") });
    card.appendChild(h("div", { class: "item-head" },
      h("div", null,
        h("div", { class: "item-title", text: item.name + (item.dose ? " — " + item.dose : "") }),
        item.notes ? h("div", { class: "item-sub", text: item.notes }) : null
      ),
      item.active ? intakeBadge(item) : badge("wyłączony", "")
    ));
    var actions = h("div", { class: "item-actions" });
    if (item.active) {
      var taken = h("button", { class: "btn btn-primary btn-sm", type: "button" }, "Wzięte");
      taken.addEventListener("click", function () {
        withBusy(taken, async function () {
          await api("/supplements/" + item.id + "/intakes", { method: "POST", json: { status: "taken" } });
          toast("Zapisano: " + item.name + " wzięte.", "success");
          await loadSupplements();
        });
      });
      var skipped = h("button", { class: "btn btn-sm", type: "button" }, "Pominięte");
      skipped.addEventListener("click", function () {
        withBusy(skipped, async function () {
          await api("/supplements/" + item.id + "/intakes", { method: "POST", json: { status: "skipped" } });
          toast("Zapisano: " + item.name + " pominięte.");
          await loadSupplements();
        });
      });
      actions.appendChild(taken);
      actions.appendChild(skipped);
    }
    var edit = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Edytuj");
    edit.addEventListener("click", function () {
      withBusy(edit, async function () {
        var values = await editDialog("Edytuj suplement", [
          { name: "name", label: "Nazwa", value: item.name, maxlength: 128, required: true, wide: true },
          { name: "dose", label: "Dawka", value: item.dose || "", maxlength: 128 },
          { name: "notes", label: "Notatka", value: item.notes || "", maxlength: 500 },
        ]);
        if (!values) return;
        if (!values.name.trim()) throw new ApiError("Nazwa suplementu jest wymagana.");
        await api("/supplements/" + item.id, { method: "PATCH", json: { name: values.name, dose: values.dose, notes: values.notes } });
        toast("Zapisano zmiany.", "success");
        await loadSupplements();
      });
    });
    actions.appendChild(edit);
    var toggle = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, item.active ? "Wyłącz" : "Włącz ponownie");
    toggle.addEventListener("click", function () {
      withBusy(toggle, async function () {
        await api("/supplements/" + item.id, { method: "PATCH", json: { active: !item.active } });
        toast(item.active ? "Wyłączono " + item.name + ". Historia przyjęć zostaje." : "Włączono " + item.name + ".");
        await loadSupplements();
      });
    });
    actions.appendChild(toggle);
    card.appendChild(actions);
    return card;
  }

  function renderSupplements(items) {
    state.supplements = items;
    var container = $("supplements");
    clear(container);
    if (!items.length) {
      renderStatus("supplementsStatus", "empty", "Nie ma jeszcze żadnych suplementów. Dodaj pierwszy powyżej albo napisz do bota na Telegramie.");
      return;
    }
    var active = items.filter(function (i) { return i.active; });
    var inactive = items.filter(function (i) { return !i.active; });
    active.forEach(function (item) { container.appendChild(supplementCard(item)); });
    if (inactive.length) {
      container.appendChild(h("h2", { text: "Wyłączone" }));
      inactive.forEach(function (item) { container.appendChild(supplementCard(item)); });
    }
  }

  function loadSupplements() {
    return loadInto("supplementsStatus", async function () {
      renderSupplements(await api("/supplements"));
    });
  }

  function setupSupplementForm() {
    var form = $("supplementForm");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = form.querySelector('button[type="submit"]');
      withBusy(button, async function () {
        var name = $("supName").value.trim();
        if (!name) { $("supName").focus(); throw new ApiError("Podaj nazwę suplementu."); }
        var result = await api("/supplements", { method: "POST", json: {
          name: name, dose: $("supDose").value.trim() || null, notes: $("supNotes").value.trim() || null,
        } });
        toast(result.message || "Dodano suplement.", "success");
        form.reset();
        form.closest("details").open = false;
        await loadSupplements();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Przypomnienia
  // ---------------------------------------------------------------------------

  function weekdaysSummary(schedule) {
    var days = (schedule && schedule.weekdays) || [0, 1, 2, 3, 4, 5, 6];
    if (days.length === 7) return "codziennie";
    if (days.length === 5 && [0, 1, 2, 3, 4].every(function (d) { return days.indexOf(d) >= 0; })) return "w dni robocze";
    return days.slice().sort().map(function (d) { return WEEKDAYS[d]; }).join(", ");
  }

  function conditionSummary(rule) {
    if (!rule.condition_type) return "zawsze";
    var text = CONDITION_LABELS[rule.condition_type] || rule.condition_type;
    if (rule.condition_threshold != null) {
      text += " " + fmt(rule.condition_threshold, 0) + (rule.condition_type === "protein_below" ? " g" : "");
    }
    if (rule.condition_window_hours && (rule.condition_type === "no_run" || rule.condition_type === "no_workout")) {
      text += " przez " + rule.condition_window_hours + " h";
    }
    return "tylko gdy " + text;
  }

  function supplementName(rule) {
    var id = rule.payload && rule.payload.supplement_id;
    if (!id) return null;
    var found = state.supplements.filter(function (s) { return s.id === id; })[0];
    return found ? found.name : "suplement #" + id;
  }

  function reminderCard(rule) {
    var status = RULE_STATUS[rule.status] || { label: rule.status, cls: "" };
    var card = h("div", { class: "item" + (rule.status === "paused" ? " is-inactive" : "") });
    var supplement = supplementName(rule);
    card.appendChild(h("div", { class: "item-head" },
      h("div", null,
        h("div", { class: "item-title", text: rule.title }),
        h("div", { class: "item-sub", text: rule.local_time + " · " + weekdaysSummary(rule.schedule) + " · " + conditionSummary(rule) }),
        h("div", { class: "item-meta", text: (KIND_LABELS[rule.kind] || rule.kind) + (supplement ? " · " + supplement : "") +
          (rule.timezone && rule.timezone !== "Europe/Warsaw" ? " · " + rule.timezone : "") })
      ),
      badge(status.label, status.cls)
    ));
    var actions = h("div", { class: "item-actions" });
    if (rule.status === "draft") {
      var activate = h("button", { class: "btn btn-primary btn-sm", type: "button" }, "Aktywuj");
      activate.addEventListener("click", function () {
        withBusy(activate, async function () {
          await api("/reminders/" + rule.id + "/activate", { method: "POST" });
          toast("Aktywowano przypomnienie „" + rule.title + "”.", "success");
          await loadReminders();
        });
      });
      actions.appendChild(activate);
    } else {
      var next = rule.status === "active" ? "paused" : "active";
      var toggle = h("button", { class: "btn btn-sm", type: "button" }, rule.status === "active" ? "Wstrzymaj" : "Wznów");
      toggle.addEventListener("click", function () {
        withBusy(toggle, async function () {
          await api("/reminders/" + rule.id, { method: "PATCH", json: { status: next } });
          toast(next === "paused" ? "Wstrzymano przypomnienie." : "Wznowiono przypomnienie.", "success");
          await loadReminders();
        });
      });
      actions.appendChild(toggle);
    }
    var edit = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Edytuj");
    edit.addEventListener("click", function () {
      withBusy(edit, async function () {
        var values = await editDialog("Edytuj przypomnienie", [
          { name: "title", label: "Treść", value: rule.title, maxlength: 256, required: true, wide: true },
          { name: "local_time", label: "Godzina", type: "time", value: rule.local_time, required: true },
          { name: "condition_type", label: "Warunek", type: "select", value: rule.condition_type || "", options: [
            { value: "", label: "Zawsze" }, { value: "steps_below", label: "Kroki poniżej progu" },
            { value: "protein_below", label: "Białko poniżej progu" }, { value: "no_run", label: "Brak biegu" },
            { value: "no_workout", label: "Brak treningu" },
          ] },
          { name: "condition_threshold", label: "Próg (kroki lub gramy białka)", type: "number", value: rule.condition_threshold, min: 1, step: "any" },
          { name: "condition_window_hours", label: "Okno wstecz w godzinach (brak biegu/treningu)", type: "number", value: rule.condition_window_hours, min: 1, max: 168, step: 1 },
          { name: "weekdays", label: "Dni tygodnia", type: "weekdays", value: (rule.schedule && rule.schedule.weekdays) || [0, 1, 2, 3, 4, 5, 6] },
        ]);
        if (!values) return;
        if (!values.title.trim()) throw new ApiError("Treść przypomnienia jest wymagana.");
        if (!values.weekdays.length) throw new ApiError("Wybierz co najmniej jeden dzień tygodnia.");
        var condition = values.condition_type || null;
        var payload = {
          title: values.title,
          local_time: values.local_time,
          condition_type: condition,
          condition_threshold: condition === "steps_below" || condition === "protein_below" ? values.condition_threshold : null,
          condition_window_hours: condition === "no_run" || condition === "no_workout" ? values.condition_window_hours : null,
          schedule_json: { weekdays: values.weekdays },
        };
        await api("/reminders/" + rule.id, { method: "PATCH", json: payload });
        toast("Zapisano zmiany.", "success");
        await loadReminders();
      });
    });
    actions.appendChild(edit);
    card.appendChild(actions);
    return card;
  }

  function renderUnknownOutbox(items) {
    var container = $("outboxUnknown");
    clear(container);
    items.forEach(function (item) {
      var card = h("div", { class: "item" });
      card.appendChild(h("div", { class: "item-head" },
        h("div", null,
          h("div", { class: "item-title", text: item.title }),
          h("div", { class: "item-sub", text: "Zaplanowane na " + fmtDateTime(item.scheduled_for) + ". Wysyłka na Telegram została przerwana i nie wiadomo, czy wiadomość dotarła." }),
          item.last_error ? h("div", { class: "item-meta", text: "Błąd: " + item.last_error }) : null
        ),
        badge("Niepewna wysyłka", "badge-warn")
      ));
      var retry = h("button", { class: "btn btn-sm", type: "button" }, "Wyślij ponownie");
      retry.addEventListener("click", function () {
        withBusy(retry, async function () {
          var ok = await confirmAction({
            title: "Wysłać ponownie?",
            text: "Jeśli pierwsza wiadomość jednak dotarła, na Telegramie pojawi się duplikat.",
            confirmLabel: "Wyślij ponownie", danger: false,
          });
          if (!ok) return;
          await api("/outbox/" + item.id + "/retry", { method: "POST" });
          toast("Ustawiono ponowną wysyłkę. Scheduler wyśle ją przy najbliższym cyklu.", "success");
          await loadReminders();
        });
      });
      card.appendChild(h("div", { class: "item-actions" }, retry));
      container.appendChild(card);
    });
  }

  function renderReminderHistory(items) {
    var container = $("reminderHistory");
    clear(container);
    if (!items.length) {
      container.appendChild(h("p", { class: "hint", text: "Jeszcze nic nie zostało zaplanowane. Wystąpienia pojawią się po pierwszej godzinie aktywnej reguły." }));
      return;
    }
    container.appendChild(h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", { text: "Termin" }), h("th", { text: "Przypomnienie" }), h("th", { text: "Stan" }))),
      h("tbody", null, items.map(function (item) {
        var label = OCCURRENCE_STATUS[item.status] || item.status;
        var cls = item.status === "delivered" || item.status === "completed" ? "badge-good"
          : item.status === "delivery_unknown" ? "badge-warn" : "";
        return h("tr", null,
          h("td", { text: fmtDateTime(item.scheduled_for) }),
          h("td", { text: item.title }),
          h("td", null, badge(label, cls))
        );
      }))
    )));
  }

  function renderReminders(rules) {
    var container = $("reminders");
    clear(container);
    if (!rules.length) {
      container.appendChild(h("div", { class: "status status-empty" },
        "Nie ma jeszcze żadnych reguł. Utwórz szkic powyżej albo poproś bota na Telegramie."));
      return;
    }
    var order = { draft: 0, active: 1, paused: 2 };
    rules.slice().sort(function (a, b) { return (order[a.status] || 0) - (order[b.status] || 0) || a.id - b.id; })
      .forEach(function (rule) { container.appendChild(reminderCard(rule)); });
  }

  function loadReminders() {
    return loadInto("remindersStatus", async function () {
      var results = await Promise.all([
        api("/reminders"), api("/outbox/unknown"), api("/reminders/history?limit=30"), api("/supplements"),
      ]);
      state.supplements = results[3];
      fillSupplementSelect();
      renderUnknownOutbox(results[1]);
      renderReminders(results[0]);
      renderReminderHistory(results[2]);
    });
  }

  function fillSupplementSelect() {
    var select = $("remSupplement");
    var previous = select.value;
    clear(select);
    select.appendChild(h("option", { value: "" }, "— bez powiązania —"));
    state.supplements.filter(function (s) { return s.active; }).forEach(function (s) {
      select.appendChild(h("option", { value: String(s.id) }, s.name + (s.dose ? " (" + s.dose + ")" : "")));
    });
    select.value = previous;
  }

  function syncReminderFormFields() {
    var kind = $("remKind").value;
    var condition = $("remCondition").value;
    $("remSupplementField").hidden = kind !== "supplement";
    var needsThreshold = condition === "steps_below" || condition === "protein_below";
    $("remThresholdField").hidden = !needsThreshold;
    $("remThresholdLabel").textContent = condition === "protein_below" ? "Próg białka (g)" : "Próg kroków";
    $("remWindowField").hidden = !(condition === "no_run" || condition === "no_workout");
  }

  function setupReminderForm() {
    var form = $("reminderForm");
    $("remKind").addEventListener("change", syncReminderFormFields);
    $("remCondition").addEventListener("change", syncReminderFormFields);
    syncReminderFormFields();
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = form.querySelector('button[type="submit"]');
      withBusy(button, async function () {
        var title = $("remTitle").value.trim();
        if (!title) { $("remTitle").focus(); throw new ApiError("Wpisz treść przypomnienia."); }
        var condition = $("remCondition").value || null;
        var threshold = $("remThreshold").value === "" ? null : Number($("remThreshold").value);
        var windowHours = $("remWindow").value === "" ? null : Number($("remWindow").value);
        if ((condition === "steps_below" || condition === "protein_below") && !(threshold > 0)) {
          $("remThreshold").focus();
          throw new ApiError("Podaj dodatni próg dla tego warunku.");
        }
        var supplementId = $("remKind").value === "supplement" && $("remSupplement").value ? Number($("remSupplement").value) : null;
        var result = await api("/reminders", { method: "POST", json: {
          title: title,
          kind: $("remKind").value,
          local_time: $("remTime").value || "20:00",
          condition_type: condition,
          condition_threshold: condition === "steps_below" || condition === "protein_below" ? threshold : null,
          condition_window_hours: condition === "no_run" || condition === "no_workout" ? windowHours : null,
          supplement_id: supplementId,
        } });
        toast("Utworzono szkic #" + result.id + ". Aktywuj go na liście, żeby zaczął działać.", "success");
        form.reset();
        syncReminderFormFields();
        form.closest("details").open = false;
        await loadReminders();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Proaktywne alerty
  // ---------------------------------------------------------------------------

  function renderAlerts(data) {
    var container = $("alerts");
    clear(container);
    data.settings.forEach(function (item) {
      var evaluation = EVALUATION_STATUS[item.evaluation.status] || { label: item.evaluation.status, cls: "" };
      var card = h("div", { class: "item" });
      var checkbox = h("input", { type: "checkbox", checked: !!item.enabled, "aria-label": "Włącz temat: " + item.label });
      checkbox.addEventListener("change", async function () {
        var enabled = checkbox.checked;
        checkbox.disabled = true;
        try {
          await api("/proactive-alerts/" + item.topic, { method: "PATCH", json: { enabled: enabled } });
          toast(enabled ? "Włączono: " + item.label + "." : "Wyłączono: " + item.label + ". Oczekujące wysyłki anulowano.", "success");
          await loadAlerts();
        } catch (err) {
          checkbox.checked = !enabled;
          toast(err.message, "error");
        } finally {
          checkbox.disabled = false;
        }
      });
      card.appendChild(h("div", { class: "item-head" },
        h("div", null,
          h("div", { class: "item-title", text: item.label }),
          h("div", { class: "item-sub", text: "Dzisiejsza ocena: " + item.evaluation.reason })
        ),
        badge(evaluation.label, evaluation.cls)
      ));
      card.appendChild(h("label", { class: "switch" }, checkbox, h("span", { text: item.enabled ? "Włączony" : "Wyłączony" })));
      container.appendChild(card);
    });

    var history = $("alertHistory");
    clear(history);
    if (!data.history.length) {
      history.appendChild(h("p", { class: "hint", text: "Żaden temat nie zakwalifikował się jeszcze do wysyłki." }));
      return;
    }
    history.appendChild(h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", { text: "Data" }), h("th", { text: "Temat" }), h("th", { text: "Wysyłka" }))),
      h("tbody", null, data.history.map(function (item) {
        var status = OUTBOX_STATUS[item.delivery_status] || { label: item.delivery_status, cls: "" };
        return h("tr", null,
          h("td", { text: fmtDateTime(item.qualified_at) }),
          h("td", { text: item.label }),
          h("td", null, badge(status.label, status.cls))
        );
      }))
    )));
  }

  function loadAlerts() {
    return loadInto("alertsStatus", async function () {
      renderAlerts(await api("/proactive-alerts"));
    });
  }

  // ---------------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------------

  function setupControls() {
    document.querySelectorAll("[data-days]").forEach(function (button) {
      button.addEventListener("click", function () {
        state.days = Number(button.dataset.days);
        loadOverview();
      });
    });
    document.querySelectorAll("#photoViewFilter button").forEach(function (button) {
      button.addEventListener("click", function () {
        state.photoView = button.dataset.view;
        loadPhotos();
      });
    });
    $("compareClear").addEventListener("click", function () {
      state.selectedPhotos = [];
      togglePhotoSelection(-1, false);
    });
    $("logoutButton").addEventListener("click", function () {
      withBusy($("logoutButton"), async function () {
        await api("/logout", { method: "POST" });
        location.replace("/dash");
      });
    });
    window.addEventListener("hashchange", route);
    setupPhotoForm();
    setupSupplementForm();
    setupReminderForm();
  }

  async function boot() {
    try {
      var session = await api("/session");
      state.csrf = session.csrf;
      state.today = session.today;
      state.timezone = session.timezone || state.timezone;
    } catch (err) {
      if (err.status !== 401) toast(err.message, "error");
      return;
    }
    $("photoDate").value = state.today;
    setupControls();
    if (!currentSection()) history.replaceState(null, "", "#przeglad");
    route();
  }

  boot();
})();
