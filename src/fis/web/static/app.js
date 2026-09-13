"use strict";

(function () {
  var pageId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
  var tablePosition = { left: 0, top: 0 };
  var activeBulkField = null;
  var bulkLabels = {
    channel: "运输渠道",
    delivery_date: "提货日期",
    sailing_date: "开船日期",
    delivery_warehouse: "交仓仓库",
  };

  function sections() {
    return document.getElementById("workspace-sections");
  }

  function rememberTablePosition() {
    var scroller = document.querySelector("[data-table-scroll]");
    if (scroller) {
      tablePosition.left = scroller.scrollLeft;
      tablePosition.top = scroller.scrollTop;
    }
  }

  function restoreTablePosition() {
    var scroller = document.querySelector("[data-table-scroll]");
    if (scroller) {
      scroller.scrollLeft = tablePosition.left;
      scroller.scrollTop = tablePosition.top;
    }
  }

  function swapFragment(html) {
    rememberTablePosition();
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
    restoreTablePosition();
    refreshGenerationButton();
  }

  async function fisSend(url, options) {
    options = options || {};
    var headers = new Headers(options.headers || {});
    headers.set("X-FIS-Request", "1");
    return fetch(url, {
      method: options.method || "POST",
      headers: headers,
      body: options.body || null,
      keepalive: Boolean(options.keepalive),
    });
  }

  function setMessage(message, failed) {
    var node = document.getElementById("workspace-status") || document.getElementById("upload-status");
    if (node) {
      node.textContent = message || "";
      node.classList.toggle("failed", Boolean(failed));
    }
  }

  async function applyResponse(response, statusNode) {
    if (response.status === 204) {
      return true;
    }
    var body = await response.text();
    if (response.ok) {
      swapFragment(body);
      return true;
    }
    if (statusNode) {
      statusNode.textContent = body;
    } else {
      setMessage(body, true);
    }
    return false;
  }

  function jsonFromForm(form) {
    var payload = {};
    var shipments = {};
    var hasShipment = false;
    new FormData(form).forEach(function (value, key) {
      var match = /^ship:(.+):([^:]+)$/.exec(key);
      if (match) {
        hasShipment = true;
        shipments[match[1]] = shipments[match[1]] || {};
        shipments[match[1]][match[2]] = value;
      } else {
        payload[key] = value;
      }
    });
    if (hasShipment) {
      payload.shipments = shipments;
    }
    return JSON.stringify(payload);
  }

  function setSaveState(text, failed) {
    var node = document.getElementById("save-status");
    if (node) {
      node.textContent = text;
      node.classList.toggle("failed", Boolean(failed));
    }
  }

  function updateCompletion(complete, total) {
    var badge = document.getElementById("completion-badge");
    if (badge) {
      badge.dataset.completeCount = String(complete);
      badge.dataset.totalCount = String(total);
      badge.textContent = complete + " / " + total + " 完整";
      badge.classList.toggle("complete", complete === total);
      badge.classList.toggle("incomplete", complete !== total);
    }
    var text = document.querySelector("[data-completion-text]");
    if (text) {
      text.textContent = complete + " / " + total;
    }
    refreshGenerationButton();
  }

  function makeInlineAddress(row, cell) {
    cell.innerHTML = "";
    var wrap = document.createElement("div");
    wrap.className = "inline-address";
    var input = document.createElement("input");
    input.setAttribute("data-inline-address", "");
    input.setAttribute("aria-label", row.dataset.shipmentRow + " 仓库完整地址");
    input.placeholder = "填写完整地址";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "text-button";
    button.setAttribute("data-save-address", "");
    button.textContent = "保存到主数据";
    wrap.append(input, button);
    cell.appendChild(wrap);
  }

  function updateRow(rowData) {
    var row = document.querySelector('[data-shipment-row="' + CSS.escape(rowData.shipment_id) + '"]');
    if (!row) {
      return;
    }
    Object.keys(rowData.fields).forEach(function (field) {
      var control = row.querySelector('[data-cell-save][data-field="' + field + '"]');
      if (control) {
        control.value = rowData.fields[field] || "";
      }
    });
    var addressCell = row.querySelector(".address-cell");
    if (addressCell) {
      if (rowData.fields.warehouse_address) {
        addressCell.textContent = rowData.fields.warehouse_address;
      } else if (!addressCell.querySelector("[data-inline-address]")) {
        makeInlineAddress(row, addressCell);
      }
      addressCell.classList.toggle("field-incomplete", Boolean(rowData.errors.warehouse_address));
    }
    var status = row.querySelector(".row-status");
    if (status) {
      status.textContent = rowData.complete ? "完整" : "待补充";
      status.classList.toggle("complete", rowData.complete);
      status.classList.toggle("incomplete", !rowData.complete);
    }
    row.classList.toggle("row-complete", rowData.complete);
    row.classList.toggle("row-incomplete", !rowData.complete);
    Array.prototype.forEach.call(row.querySelectorAll("[data-field]"), function (control) {
      var cell = control.closest("td");
      if (cell) {
        cell.classList.toggle("field-incomplete", Boolean(rowData.errors[control.dataset.field]));
      }
    });
  }

  function refreshGenerationButton() {
    var badge = document.getElementById("completion-badge");
    var folder = document.getElementById("output-folder");
    var button = document.querySelector("#generation-action .generate");
    if (!badge || !folder || !button) {
      return;
    }
    button.disabled = badge.dataset.completeCount !== badge.dataset.totalCount || folder.dataset.folderSelected !== "true";
  }

  async function saveCell(control) {
    var row = control.closest("[data-shipment-row]");
    var editor = control.closest("#shipment-editor");
    if (!row || !editor) {
      return;
    }
    setSaveState("保存中…", false);
    var response = await fisSend(
      "/workspace/" + encodeURIComponent(editor.dataset.batchId) + "/shipments/" + encodeURIComponent(row.dataset.shipmentRow),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ field: control.dataset.field, value: control.value }),
      }
    );
    var body;
    try {
      body = await response.json();
    } catch (error) {
      setSaveState(await response.text(), true);
      return;
    }
    if (!response.ok) {
      setSaveState(body.detail || "保存失败", true);
      return;
    }
    updateRow(body.row);
    updateCompletion(body.complete_count, body.total_count);
    setSaveState("已保存", false);
  }

  function openBulk(trigger) {
    activeBulkField = trigger.dataset.bulkField;
    var popover = document.getElementById("bulk-popover");
    if (!popover) {
      return;
    }
    var label = bulkLabels[activeBulkField];
    popover.querySelector("[data-bulk-title]").textContent = "批量设置" + label;
    popover.querySelector("[data-bulk-label]").textContent = label;
    var host = popover.querySelector("[data-bulk-control]");
    host.innerHTML = "";
    var source = document.querySelector('[data-cell-save][data-field="' + activeBulkField + '"]');
    var input = source && source.tagName === "SELECT" ? source.cloneNode(true) : document.createElement("input");
    input.id = "bulk-value";
    input.removeAttribute("data-cell-save");
    input.removeAttribute("aria-label");
    if (activeBulkField.indexOf("date") !== -1) {
      input.type = "date";
    }
    host.appendChild(input);
    var rect = trigger.getBoundingClientRect();
    popover.style.left = Math.max(16, Math.min(window.innerWidth - 292, rect.left - 80)) + "px";
    popover.style.top = Math.min(window.innerHeight - 230, rect.bottom + 8) + "px";
    popover.hidden = false;
    input.focus();
  }

  async function confirmBulk() {
    var popover = document.getElementById("bulk-popover");
    var editor = document.getElementById("shipment-editor");
    var input = popover && popover.querySelector("#bulk-value");
    if (!popover || !editor || !input || !activeBulkField) {
      return;
    }
    setSaveState("保存中…", false);
    var response = await fisSend("/workspace/" + encodeURIComponent(editor.dataset.batchId) + "/bulk", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field: activeBulkField, value: input.value }),
    });
    var body = await response.json();
    if (!response.ok) {
      setSaveState(body.detail || "批量保存失败", true);
      return;
    }
    body.rows.forEach(updateRow);
    updateCompletion(body.complete_count, body.total_count);
    popover.hidden = true;
    setSaveState("已保存", false);
  }

  function openDrawer(id) {
    rememberTablePosition();
    var drawer = document.getElementById(id);
    var backdrop = document.getElementById("drawer-backdrop");
    if (drawer && backdrop) {
      drawer.classList.add("open");
      drawer.setAttribute("aria-hidden", "false");
      backdrop.hidden = false;
    }
  }

  function closeDrawers() {
    Array.prototype.forEach.call(document.querySelectorAll(".drawer.open"), function (drawer) {
      drawer.classList.remove("open");
      drawer.setAttribute("aria-hidden", "true");
    });
    var backdrop = document.getElementById("drawer-backdrop");
    if (backdrop) {
      backdrop.hidden = true;
    }
    restoreTablePosition();
  }

  var DEFAULT_FILE_LABEL = "选择 Excel 文件";

  function setFileLabel(name) {
    var label = document.querySelector("[data-file-label]");
    var picker = document.querySelector(".file-picker");
    if (!label || !picker) {
      return;
    }
    label.textContent = name ? "已选择：" + name : DEFAULT_FILE_LABEL;
    picker.classList.toggle("has-file", Boolean(name));
  }

  function setUploadStatus(status, message, isError) {
    if (!status) {
      return;
    }
    status.textContent = message;
    status.classList.toggle("is-error", Boolean(isError));
  }

  var invoiceInput = document.getElementById("invoice");
  if (invoiceInput) {
    invoiceInput.addEventListener("change", function () {
      var file = invoiceInput.files && invoiceInput.files[0];
      setFileLabel(file ? file.name : "");
      setUploadStatus(document.getElementById("upload-status"), "", false);
    });
  }

  var uploadForm = document.getElementById("invoice-form");
  if (uploadForm) {
    uploadForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var status = document.getElementById("upload-status");
      setUploadStatus(status, "正在上传并解析…", false);
      var response = await fisSend("/workspace/invoice", { body: new FormData(uploadForm) });
      if (await applyResponse(response, status)) {
        setUploadStatus(status, "", false);
        setFileLabel("");
        uploadForm.reset();
        document.getElementById("upload").classList.add("is-collapsed");
      } else {
        status.classList.add("is-error");
      }
    });
  }

  // The masters drawer is re-rendered on every change, so its import form is
  // bound by delegation rather than by a reference taken at load time.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || input.id !== "masters-file") {
      return;
    }
    var label = document.querySelector("[data-masters-file-label]");
    var file = input.files && input.files[0];
    if (label) {
      label.textContent = file ? "已选择：" + file.name : "选择主数据表格";
    }
  });

  document.addEventListener("submit", async function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || form.id !== "masters-import-form") {
      return;
    }
    event.preventDefault();
    var status = document.getElementById("masters-import-status");
    if (status) {
      status.textContent = "正在导入…";
      status.classList.remove("is-error");
    }
    var response = await fisSend("/masters/import", { body: new FormData(form) });
    var body = await response.text();
    if (response.ok) {
      var target = document.getElementById("masters-body");
      if (target) {
        target.innerHTML = body;
      }
      return;
    }
    if (status) {
      status.textContent = body;
      status.classList.add("is-error");
    }
  });

  document.addEventListener("change", function (event) {
    var control = event.target.closest && event.target.closest("[data-cell-save]");
    if (control) {
      void saveCell(control);
    }
  });

  document.addEventListener("submit", async function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.fis) {
      return;
    }
    event.preventDefault();
    var options = {};
    if (form.dataset.fis === "json") {
      options.headers = { "Content-Type": "application/json" };
      options.body = jsonFromForm(form);
    }
    var response = await fisSend(form.action, options);
    await applyResponse(response, null);
  });

  document.addEventListener("click", async function (event) {
    var trigger = event.target.closest && event.target.closest("button, [data-fis-load]");
    if (!trigger) {
      return;
    }
    if (trigger.matches("[data-product-toggle]")) {
      var all = trigger.parentElement.querySelector(".product-all");
      var expanded = trigger.getAttribute("aria-expanded") === "true";
      trigger.setAttribute("aria-expanded", String(!expanded));
      trigger.hidden = !expanded;
      all.hidden = expanded;
      return;
    }
    if (trigger.matches(".product-all")) {
      return;
    }
    if (trigger.dataset.bulkField) {
      openBulk(trigger);
      return;
    }
    if (trigger.matches("[data-bulk-cancel]")) {
      document.getElementById("bulk-popover").hidden = true;
      return;
    }
    if (trigger.matches("[data-bulk-clear]")) {
      document.getElementById("bulk-value").value = "";
      return;
    }
    if (trigger.matches("[data-bulk-confirm]")) {
      await confirmBulk();
      return;
    }
    if (trigger.matches("[data-save-address]")) {
      var row = trigger.closest("[data-shipment-row]");
      var editor = trigger.closest("#shipment-editor");
      var address = row.querySelector("[data-inline-address]").value;
      setSaveState("保存中…", false);
      var addressResponse = await fisSend(
        "/workspace/" + encodeURIComponent(editor.dataset.batchId) + "/shipments/" + encodeURIComponent(row.dataset.shipmentRow) + "/warehouse-address",
        { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address: address }) }
      );
      if (!addressResponse.ok) {
        setSaveState(await addressResponse.text(), true);
        return;
      }
      var addressBody = await addressResponse.json();
      updateRow(addressBody.row);
      updateCompletion(addressBody.complete_count, addressBody.total_count);
      setSaveState("已保存", false);
      return;
    }
    if (trigger.matches("[data-reupload]")) {
      document.getElementById("upload").classList.remove("is-collapsed");
      setFileLabel("");
      setUploadStatus(document.getElementById("upload-status"), "", false);
      document.getElementById("invoice").click();
      return;
    }
    if (trigger.dataset.drawerOpen) {
      openDrawer(trigger.dataset.drawerOpen);
    }
    if (trigger.matches("[data-drawer-close]") || trigger.id === "drawer-backdrop") {
      closeDrawers();
      return;
    }
    var url = trigger.getAttribute("data-fis-load");
    if (url) {
      event.preventDefault();
      var response = await fetch(url, { headers: { "X-FIS-Request": "1" } });
      if (response.ok) {
        document.getElementById(trigger.getAttribute("data-target")).innerHTML = await response.text();
      }
    }
  });

  document.getElementById("drawer-backdrop").addEventListener("click", closeDrawers);

  async function heartbeat() {
    try {
      await fisSend("/runtime/heartbeat", {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_id: pageId }),
      });
    } catch (error) {
      // Heartbeat expiry remains the reliable shutdown path.
    }
  }

  heartbeat();
  window.setInterval(heartbeat, 5000);
  window.addEventListener("pagehide", function () {
    void fisSend("/runtime/page-closed", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_id: pageId }),
      keepalive: true,
    });
  });
})();
