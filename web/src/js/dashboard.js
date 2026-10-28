(function () {
  "use strict";

  var MAX_PAGES = 20;
  var MAX_ID = 256;
  var VIEWS = ["leaderboard", "trend", "evidence", "contrary", "reports", "status"];
  var filters = { topic: "", window: "", kind: "" };
  var ticket = 0;
  var blobs = [];

  function nextTicket() {
    ticket += 1;
    return ticket;
  }

  function credentialValue() {
    var field = document.getElementById("credential");
    if (!field || typeof field.value !== "string") return "";
    return field.value.trim();
  }

  function headers() {
    var value = credentialValue();
    var result = { Accept: "application/json" };
    if (value) result.Authorization = "Bearer " + value;
    return result;
  }

  function isHttpsUrl(value) {
    if (typeof value !== "string") return false;
    if (value.slice(0, 8).toLowerCase() !== "https://") return false;
    if (value.length < 9) return false;
    if (value.indexOf(" ") !== -1 || value.indexOf("\\") !== -1) return false;
    return true;
  }

  function isPublic(row) {
    if (!row || typeof row !== "object") return false;
    if (Object.prototype.hasOwnProperty.call(row, "qa_pass") && row.qa_pass !== true) return false;
    if (Object.prototype.hasOwnProperty.call(row, "signed") && row.signed !== true) return false;
    return true;
  }

  function listRows(payload) {
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.items)) return [];
    return payload.items.filter(isPublic);
  }

  function isEvidenceCard(row) {
    return !!row && typeof row === "object" && isPublic(row) && isHttpsUrl(row.source_url);
  }

  function visibleCount(payload) {
    if (!payload || typeof payload !== "object") return 0;
    if (Array.isArray(payload.items)) return listRows(payload).length;
    if (payload.trend) return isPublic(payload.trend) ? 1 : 0;
    if (typeof payload.source_url === "string") {
      return isPublic(payload) && isHttpsUrl(payload.source_url) ? 1 : 0;
    }
    if (typeof payload.content === "string" || Array.isArray(payload.evidence)) {
      if (!isPublic(payload)) return 0;
      var hasBody = typeof payload.content === "string" && payload.content.length > 0;
      var cards = Array.isArray(payload.evidence) ? payload.evidence.filter(isEvidenceCard) : [];
      return hasBody || cards.length > 0 ? 1 : 0;
    }
    return 0;
  }

  function flagDeep(value, depth) {
    if (!value || typeof value !== "object" || depth > 4) return false;
    var pairs = Array.isArray(value)
      ? value.map(function (item, index) {
          return [String(index), item];
        })
      : Object.entries(value);
    var index;
    for (index = 0; index < pairs.length; index += 1) {
      var key = pairs[index][0];
      var item = pairs[index][1];
      if (typeof key === "string" && key.endsWith("_degraded") && item) return true;
      if (item && typeof item === "object" && flagDeep(item, depth + 1)) return true;
    }
    return false;
  }

  function isDegradedPayload(payload) {
    if (!payload || typeof payload !== "object") return false;
    if (payload.stale === true || payload.degraded === true) return true;
    if (payload.status === "unavailable") return true;
    if (payload.checks && typeof payload.checks === "object") {
      var values = Object.values(payload.checks);
      var index;
      for (index = 0; index < values.length; index += 1) {
        if (values[index] === "fail") return true;
      }
    }
    return flagDeep(payload, 0);
  }

  function panels(httpStatus, payload) {
    if (httpStatus === "loading" || httpStatus === "error") return [httpStatus];
    if (typeof httpStatus === "number" && httpStatus !== 200 && httpStatus !== 503) {
      return ["error"];
    }
    var names = [];
    var degraded = httpStatus === 503 || isDegradedPayload(payload);
    if (degraded) names.push("degraded");
    if (visibleCount(payload) === 0) names.push("empty");
    else names.push("ready");
    return names;
  }

  function statusPanels(liveStatus, liveBody, readyStatus, readyBody) {
    var liveBad = typeof liveStatus === "number" && liveStatus !== 200 && liveStatus !== 503;
    var readyBad = typeof readyStatus === "number" && readyStatus !== 200 && readyStatus !== 503;
    if (liveStatus === "error" || readyStatus === "error" || liveBad || readyBad) return ["error"];
    var checks = readyBody && readyBody.checks;
    var hasChecks =
      !!checks && typeof checks === "object" && !Array.isArray(checks) && Object.keys(checks).length > 0;
    var liveNamed = !!(liveBody && typeof liveBody.status === "string" && liveBody.status);
    var degraded =
      liveStatus === 503 ||
      readyStatus === 503 ||
      isDegradedPayload(liveBody) ||
      isDegradedPayload(readyBody);
    if (!hasChecks && !liveNamed) return degraded ? ["degraded", "empty"] : ["empty"];
    return degraded ? ["degraded", "ready"] : ["ready"];
  }

  function validId(value) {
    if (typeof value !== "string" || value.length === 0 || value.length > MAX_ID) return false;
    if (value.indexOf("/") !== -1 || value.indexOf("\\") !== -1 || value.indexOf("..") !== -1) {
      return false;
    }
    var index;
    for (index = 0; index < value.length; index += 1) {
      if (value.charCodeAt(index) < 32) return false;
    }
    return true;
  }

  function evidenceIds(body, kind) {
    var trend = body.trend || {};
    var primary = kind === "contrary" ? body.contrary_ids : body.evidence_ids;
    var nested =
      kind === "contrary" ? trend.contrary_evidence_ids : trend.supporting_evidence_ids;
    var found = [];
    var seen = {};
    [primary, nested].forEach(function (list) {
      if (!Array.isArray(list)) return;
      list.forEach(function (item) {
        if (!validId(item) || seen[item]) return;
        seen[item] = true;
        found.push(item);
      });
    });
    return found;
  }

  async function getJson(path) {
    var response;
    try {
      response = await fetch(path, {
        method: "GET",
        headers: headers(),
        credentials: "omit",
      });
    } catch (err) {
      return { httpStatus: "error", payload: null, message: "网络请求失败" };
    }
    var text = "";
    try {
      text = await response.text();
    } catch (err) {
      return { httpStatus: "error", payload: null, message: "响应无法读取" };
    }
    var payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (err) {
        payload = null;
      }
    }
    if (payload !== null && (typeof payload !== "object" || Array.isArray(payload))) {
      return { httpStatus: "error", payload: null, message: "响应不是对象" };
    }
    var message = payload && typeof payload.message === "string" ? payload.message : "";
    if (!response.ok && response.status !== 503) {
      return {
        httpStatus: "error",
        payload: payload,
        message: message || "HTTP " + response.status,
      };
    }
    if (response.ok && payload === null) {
      return { httpStatus: "error", payload: null, message: "响应无法解析" };
    }
    return { httpStatus: response.status, payload: payload, message: message };
  }

  async function fetchList(path, query) {
    var items = [];
    var cursor = "";
    var seen = {};
    var asOf = "";
    var schema = "";
    var degraded = false;
    var page;
    for (page = 0; page < MAX_PAGES; page += 1) {
      var params = new URLSearchParams();
      if (query.topic) params.set("topic", query.topic);
      if (query.window) params.set("window", query.window);
      if (query.kind) params.set("kind", query.kind);
      if (cursor) params.set("cursor", cursor);
      var qs = params.toString();
      var result = await getJson(path + (qs ? "?" + qs : ""));
      if (result.httpStatus === "error") {
        return { httpStatus: "error", payload: null, message: result.message };
      }
      var payload = result.payload || {};
      if (result.httpStatus === 503 || isDegradedPayload(payload)) degraded = true;
      if (typeof payload.as_of === "string") asOf = payload.as_of;
      if (typeof payload.schema_version === "string") schema = payload.schema_version;
      listRows(payload).forEach(function (row) {
        items.push(row);
      });
      var next = typeof payload.next_cursor === "string" ? payload.next_cursor : "";
      if (!next || seen[next] || listRows(payload).length === 0) break;
      seen[next] = true;
      cursor = next;
    }
    var body = { items: items, as_of: asOf, stale: degraded };
    if (schema) body.schema_version = schema;
    return { httpStatus: degraded ? 503 : 200, payload: body, message: "" };
  }

  function showView(name) {
    VIEWS.forEach(function (view) {
      var node = document.getElementById("view-" + view);
      if (node) node.hidden = view !== name;
    });
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function revokeBlobs() {
    while (blobs.length) URL.revokeObjectURL(blobs.pop());
  }

  function setDisclosure(payload, row) {
    var label = document.getElementById("ai-label");
    var when = document.getElementById("as-of");
    var source = row && typeof row.generated_by_ai === "boolean" ? row : payload;
    var text = source && source.generated_by_ai === false ? "非 AI 生成" : "AI 生成";
    if (label) label.textContent = text;
    var value = "";
    if (payload && typeof payload.as_of === "string") value = payload.as_of;
    else if (row && typeof row.as_of === "string") value = row.as_of;
    if (!when) return;
    when.textContent = value ? "as_of " + value : "";
    if (value) when.setAttribute("datetime", value);
    else when.removeAttribute("datetime");
  }

  function applyPanels(viewName, names, message) {
    var view = document.getElementById("view-" + viewName);
    if (!view) return;
    ["loading", "empty", "error", "degraded", "ready"].forEach(function (state) {
      var node = view.querySelector('[data-state="' + state + '"]');
      if (!node) return;
      node.hidden = names.indexOf(state) === -1;
    });
    var show = names.indexOf("error") !== -1 || names.indexOf("degraded") !== -1;
    view.querySelectorAll("[data-detail]").forEach(function (detail) {
      detail.textContent = show ? message || "" : "";
    });
  }

  function setLoading(viewName) {
    var view = document.getElementById("view-" + viewName);
    if (view) clearNode(view.querySelector("[data-results]"));
    applyPanels(viewName, ["loading"], "");
    setDisclosure(null, null);
  }

  function paint(viewName, names, payload, message, row) {
    var view = document.getElementById("view-" + viewName);
    if (!view) return;
    revokeBlobs();
    var results = view.querySelector("[data-results]");
    clearNode(results);
    var shown = names.slice();
    var drawn = 0;
    if (shown.indexOf("ready") !== -1 && results && RENDER[viewName]) {
      drawn = RENDER[viewName](results, payload) || 0;
    }
    if (shown.indexOf("ready") !== -1 && drawn === 0) {
      shown = shown.filter(function (name) {
        return name !== "ready";
      });
      if (shown.indexOf("empty") === -1) shown.push("empty");
      clearNode(results);
    }
    applyPanels(viewName, shown, message || "");
    setDisclosure(payload, row || null);
  }

  function labelLine(payload, row) {
    var line = document.createElement("p");
    line.className = "ai-label";
    var generated = row && row.generated_by_ai === false ? "非 AI 生成" : "AI 生成";
    var asOf = "";
    if (payload && typeof payload.as_of === "string" && payload.as_of) asOf = payload.as_of;
    else if (row && typeof row.as_of === "string") asOf = row.as_of;
    line.textContent = asOf ? generated + " · as_of " + asOf : generated;
    return line;
  }

  function sourceLink(url) {
    if (!isHttpsUrl(url)) return null;
    var anchor = document.createElement("a");
    anchor.href = url;
    anchor.textContent = url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  function trendText(row) {
    var parts = [];
    if (row.window_days === 7 || row.window_days === 28) parts.push(String(row.window_days) + " 天");
    if (typeof row.score === "number" && Number.isFinite(row.score)) {
      parts.push("分数 " + String(row.score));
    }
    if (typeof row.confidence === "string" && row.confidence) parts.push(row.confidence);
    return parts.join(" · ") || row.trend_id;
  }

  function renderLeaderboard(host, payload) {
    var rows = listRows(payload).filter(function (row) {
      return typeof row.topic === "string" && row.topic && validId(row.trend_id);
    });
    var grouped = new Map();
    rows.forEach(function (row) {
      var list = grouped.get(row.topic) || [];
      list.push(row);
      grouped.set(row.topic, list);
    });
    var topics = Array.from(grouped.keys()).sort(function (left, right) {
      return left.localeCompare(right);
    });
    topics.forEach(function (topic) {
      var item = document.createElement("li");
      item.className = "card";
      var title = document.createElement("h2");
      title.textContent = topic;
      item.appendChild(title);
      var ranked = grouped.get(topic).slice().sort(function (left, right) {
        var leftScore =
          typeof left.score === "number" && Number.isFinite(left.score)
            ? left.score
            : Number.NEGATIVE_INFINITY;
        var rightScore =
          typeof right.score === "number" && Number.isFinite(right.score)
            ? right.score
            : Number.NEGATIVE_INFINITY;
        if (leftScore === rightScore) return String(left.trend_id).localeCompare(String(right.trend_id));
        return rightScore - leftScore;
      });
      ranked.forEach(function (row) {
        var link = document.createElement("a");
        link.href = "#/trends/" + encodeURIComponent(row.trend_id);
        link.textContent = trendText(row);
        item.appendChild(link);
      });
      item.appendChild(labelLine(payload, null));
      host.appendChild(item);
    });
    return topics.length;
  }

  function renderTrend(host, payload) {
    var row = payload && payload.trend;
    if (!row || !isPublic(row) || !validId(row.trend_id)) return 0;
    var card = document.createElement("article");
    card.className = "card";
    var title = document.createElement("h2");
    title.textContent = typeof row.topic === "string" && row.topic ? row.topic : row.trend_id;
    card.appendChild(title);
    if (typeof row.claim === "string" && row.claim) {
      var claim = document.createElement("p");
      claim.textContent = row.claim;
      card.appendChild(claim);
    }
    var meta = document.createElement("p");
    meta.textContent = trendText(row);
    card.appendChild(meta);
    if (Array.isArray(row.source_families)) {
      var families = row.source_families.filter(function (item) {
        return typeof item === "string" && item;
      });
      if (families.length) {
        var familyLine = document.createElement("p");
        familyLine.textContent = families.join(" · ");
        card.appendChild(familyLine);
      }
    }
    var features = payload.features;
    if (features && typeof features === "object" && !Array.isArray(features)) {
      Object.keys(features)
        .sort()
        .forEach(function (key) {
          if (key === "claim") return;
          var value = features[key];
          if (value && typeof value === "object") return;
          var line = document.createElement("p");
          line.textContent = key + " · " + String(value);
          card.appendChild(line);
        });
    }
    if (typeof row.last_verified_at === "string" && row.last_verified_at) {
      var verified = document.createElement("p");
      verified.textContent = "last_verified_at " + row.last_verified_at;
      card.appendChild(verified);
    }
    if (typeof row.numeric_checks_passed === "boolean") {
      var checks = document.createElement("p");
      checks.textContent = row.numeric_checks_passed ? "数值检查已通过" : "数值检查未通过";
      card.appendChild(checks);
    }
    var evidenceNav = document.createElement("a");
    evidenceNav.href = "#/trends/" + encodeURIComponent(row.trend_id) + "/evidence";
    evidenceNav.textContent = "来源证据";
    var contraryNav = document.createElement("a");
    contraryNav.href = "#/trends/" + encodeURIComponent(row.trend_id) + "/contrary";
    contraryNav.textContent = "反证";
    card.appendChild(evidenceNav);
    card.appendChild(document.createTextNode(" "));
    card.appendChild(contraryNav);
    card.appendChild(labelLine(payload, row));
    host.appendChild(card);
    return 1;
  }

  function renderEvidenceList(host, payload) {
    var drawn = 0;
    listRows(payload)
      .filter(isEvidenceCard)
      .forEach(function (row) {
        var link = sourceLink(row.source_url);
        if (!link) return;
        var card = document.createElement("article");
        card.className = "card";
        card.appendChild(link);
        var excerpt = document.createElement("p");
        excerpt.textContent = typeof row.excerpt === "string" ? row.excerpt : "";
        card.appendChild(excerpt);
        var bits = [];
        if (typeof row.license === "string" && row.license) bits.push(row.license);
        var dates = row.dates && typeof row.dates === "object" ? row.dates : {};
        if (typeof dates.published_at === "string" && dates.published_at) {
          bits.push("published " + dates.published_at);
        }
        if (typeof dates.collected_at === "string" && dates.collected_at) {
          bits.push("collected " + dates.collected_at);
        }
        if (typeof row.hash === "string" && row.hash) bits.push(row.hash);
        var meta = document.createElement("p");
        meta.textContent = bits.join(" · ");
        card.appendChild(meta);
        card.appendChild(labelLine(row, null));
        host.appendChild(card);
        drawn += 1;
      });
    return drawn;
  }

  function fileName(reportId) {
    var cleaned = String(reportId || "")
      .replace(/[^A-Za-z0-9._-]+/g, "_")
      .slice(0, 80);
    return (cleaned || "report") + ".md";
  }

  function renderReportList(host, payload) {
    var rows = listRows(payload).filter(function (row) {
      return validId(row.report_id);
    });
    rows.forEach(function (row) {
      var item = document.createElement("article");
      item.className = "card";
      var link = document.createElement("a");
      link.href = "#/reports/" + encodeURIComponent(row.report_id);
      var kind = typeof row.report_kind === "string" ? row.report_kind : "";
      var published = typeof row.published_at === "string" ? row.published_at : "";
      link.textContent = [row.report_id, kind, published].filter(Boolean).join(" · ");
      item.appendChild(link);
      item.appendChild(labelLine(payload, row));
      host.appendChild(item);
    });
    return rows.length;
  }

  function renderReportDetail(host, payload) {
    if (!payload || !isPublic(payload)) return 0;
    var hasBody = typeof payload.content === "string" && payload.content.length > 0;
    var cards = Array.isArray(payload.evidence) ? payload.evidence.filter(isEvidenceCard) : [];
    if (!hasBody && cards.length === 0) return 0;
    var article = document.createElement("article");
    article.className = "card";
    if (hasBody) {
      var pre = document.createElement("pre");
      pre.textContent = payload.content;
      article.appendChild(pre);
      var download = document.createElement("a");
      var blob = new Blob([payload.content], { type: "text/markdown;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      blobs.push(url);
      download.href = url;
      download.download = fileName(payload.report_id);
      download.textContent = "下载";
      article.appendChild(download);
    }
    cards.forEach(function (card) {
      var link = sourceLink(card.source_url);
      if (!link) return;
      var block = document.createElement("p");
      block.appendChild(link);
      article.appendChild(block);
    });
    article.appendChild(labelLine(payload, payload));
    host.appendChild(article);
    return 1;
  }

  function renderReports(host, payload) {
    if (payload && Array.isArray(payload.items)) return renderReportList(host, payload);
    return renderReportDetail(host, payload);
  }

  function renderStatus(host, payload) {
    var count = 0;
    var live = payload && payload.live;
    var ready = payload && payload.ready;
    if (live && typeof live.status === "string" && live.status) {
      var liveLine = document.createElement("p");
      liveLine.textContent = "live · " + live.status;
      host.appendChild(liveLine);
      count += 1;
    }
    var checks = ready && ready.checks;
    if (checks && typeof checks === "object" && !Array.isArray(checks)) {
      Object.keys(checks)
        .sort()
        .forEach(function (name) {
          var line = document.createElement("p");
          line.textContent = name + " · " + String(checks[name]);
          host.appendChild(line);
          count += 1;
        });
    }
    var schema = ready && typeof ready.schema_version === "string" ? ready.schema_version : "";
    if (schema) {
      var schemaLine = document.createElement("p");
      schemaLine.textContent = "schema " + schema;
      host.appendChild(schemaLine);
    }
    if (count) host.appendChild(labelLine(payload, null));
    return count;
  }

  var RENDER = {
    leaderboard: renderLeaderboard,
    trend: renderTrend,
    evidence: renderEvidenceList,
    contrary: renderEvidenceList,
    reports: renderReports,
    status: renderStatus,
  };

  function parseRoute() {
    var raw = (location.hash || "").replace(/^#/, "");
    if (!raw || raw === "/") return { name: "leaderboard", id: "" };
    var parts = raw.split("/").filter(Boolean).map(function (part) {
      try {
        return decodeURIComponent(part);
      } catch (err) {
        return "";
      }
    });
    if (parts[0] === "status") return { name: "status", id: "" };
    if (parts[0] === "reports") return { name: "reports", id: parts[1] || "" };
    if (parts[0] === "trends" && parts[1] && parts[2] === "evidence") {
      return { name: "evidence", id: parts[1] };
    }
    if (parts[0] === "trends" && parts[1] && parts[2] === "contrary") {
      return { name: "contrary", id: parts[1] };
    }
    if (parts[0] === "trends" && parts[1]) return { name: "trend", id: parts[1] };
    return { name: "leaderboard", id: "" };
  }

  async function loadLeaderboard(mine) {
    setLoading("leaderboard");
    var result = await fetchList("/v1/trends", {
      topic: filters.topic,
      window: filters.window,
      kind: "",
    });
    if (mine !== ticket) return;
    paint("leaderboard", panels(result.httpStatus, result.payload), result.payload, result.message, null);
  }

  async function loadTrend(mine, id) {
    setLoading("trend");
    if (!validId(id)) {
      if (mine !== ticket) return;
      paint("trend", ["error"], null, "编号无效", null);
      return;
    }
    var result = await getJson("/v1/trends/" + encodeURIComponent(id));
    if (mine !== ticket) return;
    paint(
      "trend",
      panels(result.httpStatus, result.payload),
      result.payload,
      result.message,
      result.payload && result.payload.trend,
    );
  }

  async function loadLinked(mine, id, kind) {
    setLoading(kind);
    if (!validId(id)) {
      if (mine !== ticket) return;
      paint(kind, ["error"], null, "编号无效", null);
      return;
    }
    var trend = await getJson("/v1/trends/" + encodeURIComponent(id));
    if (mine !== ticket) return;
    if (trend.httpStatus === "error") {
      paint(kind, ["error"], null, trend.message, null);
      return;
    }
    var body = trend.payload || {};
    if (!body.trend || !isPublic(body.trend)) {
      var hidden = {
        items: [],
        as_of: typeof body.as_of === "string" ? body.as_of : "",
        stale: trend.httpStatus === 503 || isDegradedPayload(body),
      };
      paint(kind, panels(trend.httpStatus, hidden), hidden, trend.message, null);
      return;
    }
    var ids = evidenceIds(body, kind);
    if (ids.length === 0) {
      var emptyPayload = {
        items: [],
        as_of: typeof body.as_of === "string" ? body.as_of : "",
        stale: isDegradedPayload(body),
      };
      var emptyStatus = emptyPayload.stale || trend.httpStatus === 503 ? 503 : 200;
      paint(kind, panels(emptyStatus, emptyPayload), emptyPayload, trend.message, null);
      return;
    }
    var cards = [];
    var degraded = trend.httpStatus === 503 || isDegradedPayload(body);
    var asOf = typeof body.as_of === "string" ? body.as_of : "";
    var schema = typeof body.schema_version === "string" ? body.schema_version : "";
    var index;
    for (index = 0; index < ids.length; index += 1) {
      var item = await getJson("/v1/evidence/" + encodeURIComponent(ids[index]));
      if (mine !== ticket) return;
      if (item.httpStatus === "error") {
        if (item.payload && item.payload.code === "not_found") continue;
        paint(kind, ["error"], null, item.message, null);
        return;
      }
      if (item.httpStatus === 503 || isDegradedPayload(item.payload)) degraded = true;
      if (item.payload && isEvidenceCard(item.payload)) {
        cards.push(item.payload);
        if (!asOf && typeof item.payload.as_of === "string") asOf = item.payload.as_of;
      }
    }
    var linked = { items: cards, as_of: asOf, stale: degraded };
    if (schema) linked.schema_version = schema;
    paint(kind, panels(degraded ? 503 : 200, linked), linked, "", null);
  }

  async function loadReports(mine, id) {
    setLoading("reports");
    if (id && !validId(id)) {
      if (mine !== ticket) return;
      paint("reports", ["error"], null, "编号无效", null);
      return;
    }
    if (id) {
      var detail = await getJson("/v1/reports/" + encodeURIComponent(id));
      if (mine !== ticket) return;
      paint("reports", panels(detail.httpStatus, detail.payload), detail.payload, detail.message, detail.payload);
      return;
    }
    var result = await fetchList("/v1/reports", { topic: "", window: "", kind: filters.kind });
    if (mine !== ticket) return;
    paint("reports", panels(result.httpStatus, result.payload), result.payload, result.message, null);
  }

  async function loadStatus(mine) {
    setLoading("status");
    var live = await getJson("/health/live");
    var ready = await getJson("/health/ready");
    if (mine !== ticket) return;
    var names = statusPanels(live.httpStatus, live.payload, ready.httpStatus, ready.payload);
    var asOf = "";
    if (ready.payload && typeof ready.payload.as_of === "string") asOf = ready.payload.as_of;
    else if (live.payload && typeof live.payload.as_of === "string") asOf = live.payload.as_of;
    var payload = {
      live: live.payload,
      ready: ready.payload,
      as_of: asOf,
      stale: names.indexOf("degraded") !== -1,
    };
    paint("status", names, payload, ready.message || live.message || "", null);
  }

  function loadRoute() {
    var chosen = parseRoute();
    showView(chosen.name);
    var mine = nextTicket();
    if (chosen.name === "leaderboard") loadLeaderboard(mine);
    else if (chosen.name === "trend") loadTrend(mine, chosen.id);
    else if (chosen.name === "evidence") loadLinked(mine, chosen.id, "evidence");
    else if (chosen.name === "contrary") loadLinked(mine, chosen.id, "contrary");
    else if (chosen.name === "reports") loadReports(mine, chosen.id);
    else loadStatus(mine);
  }

  function bind() {
    var topicForm = document.getElementById("topic-filters");
    if (topicForm) {
      topicForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var field = document.getElementById("topic");
        filters.topic = field && typeof field.value === "string" ? field.value.trim() : "";
        if (location.hash !== "#/" && location.hash !== "") location.hash = "#/";
        else loadRoute();
      });
    }
    document.querySelectorAll("[data-window]").forEach(function (button) {
      button.addEventListener("click", function () {
        filters.window = button.getAttribute("data-window") || "";
        if (parseRoute().name !== "leaderboard") location.hash = "#/";
        else loadRoute();
      });
    });
    document.querySelectorAll("[data-kind]").forEach(function (button) {
      button.addEventListener("click", function () {
        filters.kind = button.getAttribute("data-kind") || "";
        if (parseRoute().name !== "reports" || parseRoute().id) location.hash = "#/reports";
        else loadRoute();
      });
    });
    var credential = document.getElementById("credential");
    if (credential) credential.addEventListener("change", loadRoute);
    window.addEventListener("hashchange", loadRoute);
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    loadRoute();
  });
})();
