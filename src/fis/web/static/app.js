"use strict";

// Minimal single-page behavior: a custom request header on every mutation, a
// five-second heartbeat, and server-rendered fragment replacement. No frontend
// framework and no remote assets. Business state lives in the backend service.
(function () {
  var pageId =
    window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Math.random());

  function sections() {
    return document.getElementById("workspace-sections");
  }

  function swapFragment(html) {
    var template = document.createElement("template");
    template.innerHTML = html.trim();
    var host = sections();
    Array.prototype.forEach.call(template.content.children, function (node) {
      var existing = node.id ? document.getElementById(node.id) : null;
      if (existing) {
        existing.replaceWith(node);
      } else if (host) {
        host.appendChild(node);
      }
    });
  }

  async function fisSend(url, options) {
    options = options || {};
    var headers = new Headers(options.headers || {});
    headers.set("X-FIS-Request", "1");
    return fetch(url, {
      method: options.method || "POST",
      headers: headers,
      body: options.body || null,
    });
  }

  async function applyResponse(response, statusNode) {
    if (response.status === 204) {
      return;
    }
    var text = await response.text();
    if (response.ok) {
      swapFragment(text);
    } else if (statusNode) {
      statusNode.textContent = text;
    } else {
      var region = document.getElementById("upload-status");
      if (region) {
        region.textContent = text;
      }
    }
  }

  function jsonFromForm(form) {
    var payload = {};
    new FormData(form).forEach(function (value, key) {
      payload[key] = value;
    });
    return JSON.stringify(payload);
  }

  var uploadForm = document.getElementById("invoice-form");
  if (uploadForm) {
    uploadForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var status = document.getElementById("upload-status");
      if (status) {
        status.textContent = "正在上传并解析…";
      }
      var response = await fisSend("/workspace/invoice", { body: new FormData(uploadForm) });
      await applyResponse(response, status);
    });
  }

  document.addEventListener("submit", async function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var mode = form.getAttribute("data-fis");
    if (!mode) {
      return;
    }
    event.preventDefault();
    var options = {};
    if (mode === "json") {
      options.headers = { "Content-Type": "application/json" };
      options.body = jsonFromForm(form);
    }
    var response = await fisSend(form.action, options);
    await applyResponse(response, null);
  });

  document.addEventListener("click", async function (event) {
    var trigger = event.target;
    if (!(trigger instanceof HTMLElement)) {
      return;
    }
    var url = trigger.getAttribute("data-fis-load");
    if (!url) {
      return;
    }
    event.preventDefault();
    var response = await fetch(url, { headers: { "X-FIS-Request": "1" } });
    if (!response.ok) {
      return;
    }
    var html = await response.text();
    var target = document.getElementById(trigger.getAttribute("data-target") || "");
    if (target) {
      target.innerHTML = html;
    }
  });

  async function heartbeat() {
    try {
      await fisSend("/runtime/heartbeat", {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_id: pageId }),
      });
    } catch (error) {
      /* transient network errors are ignored; expiry drives shutdown */
    }
  }

  heartbeat();
  window.setInterval(heartbeat, 5000);

  window.addEventListener("pagehide", function () {
    try {
      fetch("/runtime/page-closed", {
        method: "POST",
        headers: { "X-FIS-Request": "1", "Content-Type": "application/json" },
        body: JSON.stringify({ page_id: pageId }),
        keepalive: true,
      });
    } catch (error) {
      /* best effort; heartbeat expiry is the reliable path */
    }
  });
})();
