"""
api/index.py — Vercel Python function (FastAPI app).

FIX: public/index.html is now embedded directly as a Python string constant
(_FRONTEND_HTML) at the top of this file. This completely removes any
dependency on filesystem path resolution inside Vercel's bundled runtime
(/var/task/...). No os.stat(), no read_text(), no file to "find" at
request time — the HTML is just a string in memory the moment the module loads.

Endpoints:
  GET  /          -> serves the upload UI (from embedded HTML string)
  GET  /api       -> health check
  GET  /api/configs -> list available output configs
  POST /api/transform -> upload files + config, run pipeline, return JSON

NEW: the UI now has a "Configure Output…" option which lets the user tick
fields, rename them, set per-field normalization, choose on_missing
behavior (null/omit/error), and toggle confidence/provenance — all without
any code changes. The built config is sent as `config_json` (a JSON string)
alongside `config="__configure__"`, validated server-side with the same
`validate_config()` used for the two built-in presets, and fed straight
into the existing `run_pipeline()`.
"""

from __future__ import annotations
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response

from transformer.pipeline import SourceInput, run_pipeline, detect_source_kind
from transformer.projector import ProjectionError, validate_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("api.transform")

# ─── HTML embedded directly — no filesystem access at request time ────────────
# This is the ONLY reliable way to serve a static file from a Vercel Python
# function. FileResponse and read_text() both call os.stat() or open() at
# request time against a path that Vercel's bundler does not guarantee to
# preserve. Embedding the string at import time sidesteps this entirely.

_FRONTEND_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Multi-Source Candidate Data Transformer</title>
<style>
  :root {
    --paper: #FBF8F2;
    --paper-dim: #F2EEE3;
    --ink: #1C1B19;
    --ink-soft: #5B5750;
    --rule: #D8D2C4;
    --stamp-red: #A8431E;
    --stamp-green: #2F6B4F;
    --stamp-amber: #9A7B1F;
    --serif: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, serif;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: var(--sans);
    line-height: 1.5;
  }
  ::selection { background: var(--stamp-red); color: var(--paper); }
  a { color: var(--stamp-red); }
  .wrap { max-width: 920px; margin: 0 auto; padding: 48px 24px 96px; }
  header.masthead {
    border-bottom: 3px solid var(--ink);
    padding-bottom: 18px;
    margin-bottom: 36px;
  }
  .masthead .eyebrow {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-soft);
    margin-bottom: 6px;
  }
  .masthead h1 {
    font-family: var(--serif);
    font-weight: 700;
    font-size: 34px;
    margin: 0 0 6px;
    letter-spacing: -0.01em;
  }
  .masthead p { margin: 0; color: var(--ink-soft); font-size: 15px; max-width: 60ch; }
  section.intake {
    background: var(--paper-dim);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 28px;
    margin-bottom: 28px;
  }
  section.intake h2 { font-family: var(--serif); font-size: 18px; margin: 0 0 4px; }
  section.intake .sub { color: var(--ink-soft); font-size: 13px; margin: 0 0 20px; }
  .dropzone {
    border: 2px dashed var(--rule);
    border-radius: 2px;
    padding: 28px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s ease, background 0.15s ease;
    background: var(--paper);
  }
  .dropzone:hover, .dropzone.dragover { border-color: var(--stamp-red); background: #FFF9F4; }
  .dropzone .icon { font-family: var(--serif); font-size: 28px; margin-bottom: 8px; color: var(--ink-soft); }
  .dropzone .main-text { font-size: 14px; font-weight: 600; }
  .dropzone .sub-text { font-size: 12px; color: var(--ink-soft); margin-top: 4px; }
  .dropzone input[type="file"] { display: none; }
  .filelist { list-style: none; padding: 0; margin: 14px 0 0; display: flex; flex-wrap: wrap; gap: 8px; }
  .filelist li {
    font-family: var(--mono);
    font-size: 12px;
    background: var(--paper);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 5px 10px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .filelist li button { border: none; background: none; color: var(--stamp-red); cursor: pointer; font-size: 13px; line-height: 1; padding: 0; }
  .row { display: flex; gap: 20px; margin-top: 20px; flex-wrap: wrap; align-items: flex-end; }
  .field { flex: 1; min-width: 220px; }
  .field label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-soft); margin-bottom: 6px; font-weight: 600; }
  .field select, .field input[type="url"], .field input[type="text"] { width: 100%; font-family: var(--sans); font-size: 14px; padding: 9px 10px; border: 1px solid var(--rule); border-radius: 2px; background: var(--paper); color: var(--ink); }
  .field .hint { font-size: 11px; color: var(--ink-soft); margin-top: 5px; }
  button.run { font-family: var(--serif); font-weight: 700; font-size: 15px; background: var(--ink); color: var(--paper); border: none; border-radius: 2px; padding: 12px 24px; cursor: pointer; letter-spacing: 0.02em; }
  button.run:hover { background: var(--stamp-red); }
  button.run:disabled { background: var(--rule); color: var(--ink-soft); cursor: not-allowed; }
  .status-line { font-family: var(--mono); font-size: 12px; color: var(--ink-soft); margin-top: 12px; min-height: 16px; }
  .status-line.error { color: var(--stamp-red); }
  section.results h2 { font-family: var(--serif); font-size: 20px; border-bottom: 2px solid var(--ink); padding-bottom: 8px; margin: 0 0 18px; }
  .meta-line { font-family: var(--mono); font-size: 12px; color: var(--ink-soft); margin-bottom: 20px; }
  .skipped { font-size: 13px; background: #FFF6EC; border: 1px solid #E7CFA8; border-radius: 2px; padding: 10px 14px; margin-bottom: 20px; color: #6B4E14; }
  .empty-state { text-align: center; padding: 60px 20px; color: var(--ink-soft); }
  .empty-state .glyph { font-family: var(--serif); font-size: 38px; margin-bottom: 10px; }
  .card { background: var(--paper); border: 1px solid var(--rule); border-radius: 2px; margin-bottom: 20px; overflow: hidden; }
  .card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 18px 22px; border-bottom: 1px solid var(--rule); background: var(--paper-dim); }
  .card-head .name { font-family: var(--serif); font-size: 21px; font-weight: 700; }
  .card-head .headline { font-size: 13px; color: var(--ink-soft); margin-top: 3px; }
  .card-head .candidate-id { font-family: var(--mono); font-size: 10px; color: var(--ink-soft); margin-top: 6px; }
  .stamp { font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; border: 2px solid currentColor; border-radius: 3px; padding: 5px 10px; transform: rotate(-3deg); white-space: nowrap; flex-shrink: 0; }
  .stamp.verified { color: var(--stamp-green); }
  .stamp.partial  { color: var(--stamp-amber); }
  .stamp.unverified { color: var(--stamp-red); }
  .card-body { padding: 18px 22px; }
  .field-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px 24px; margin-bottom: 16px; }
  .field-grid .fg-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--ink-soft); margin-bottom: 3px; }
  .field-grid .fg-value { font-size: 14px; }
  .field-grid .fg-value.empty { color: var(--rule); font-style: italic; }
  .tag-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .tag { font-size: 12px; background: var(--paper-dim); border: 1px solid var(--rule); border-radius: 2px; padding: 3px 9px; }
  details.subsection { border-top: 1px solid var(--rule); padding-top: 12px; margin-top: 12px; }
  details.subsection summary { cursor: pointer; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-soft); list-style: none; }
  details.subsection summary::-webkit-details-marker { display: none; }
  details.subsection summary::before { content: "\\25B8 "; }
  details.subsection[open] summary::before { content: "\\25BE "; }
  .exp-entry, .edu-entry { font-size: 13px; padding: 8px 0; border-bottom: 1px dotted var(--rule); }
  .exp-entry:last-child, .edu-entry:last-child { border-bottom: none; }
  .exp-entry .role { font-weight: 600; }
  .exp-entry .dates, .edu-entry .year { color: var(--ink-soft); font-size: 12px; }
  .provenance-log { margin-top: 14px; border-top: 1px solid var(--rule); padding-top: 12px; }
  .provenance-log .pv-title { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--ink-soft); margin-bottom: 8px; }
  .pv-line { font-family: var(--mono); font-size: 11px; color: var(--ink-soft); padding: 2px 0; }
  .pv-line .pv-field { color: var(--ink); }
  .pv-line .pv-source { color: var(--stamp-red); }
  footer.page-footer { margin-top: 64px; padding-top: 20px; border-top: 1px solid var(--rule); font-size: 12px; color: var(--ink-soft); text-align: center; }
  #configurePanel table { margin-top: 4px; }
  #configurePanel th { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--ink-soft); }
  #configurePanel td, #configurePanel th { padding: 6px 6px; vertical-align: middle; }
  #configurePanel tr + tr { border-top: 1px solid var(--rule); }
  #configurePanel input[type="text"], #configurePanel select { padding: 5px 7px; font-size: 12px; }
  #configurePanel .from-path { font-family: var(--mono); font-size: 11px; color: var(--ink-soft); }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <h1>Multi-Source Candidate Data Transformer</h1>
    <p>Upload candidate information from multiple sources like Recruiter CSV, ATS JSON, Resume (PDF/DOCX) and GitHub profile. The system combines all information into one candidate profile.</p>
  </header>
  <section class="intake">
    <h2>Upload Candidate Files</h2>
    <p class="sub">Upload CSV, JSON, PDF or DOCX files. You can also enter a GitHub profile URL.</p>
    <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Upload files">
      <div class="icon">&#8962;</div>
      <div class="main-text">Upload Files or Click to Browse</div>
      <div class="sub-text">Supported files: CSV, JSON, PDF and DOCX</div>
      <input type="file" id="fileInput" multiple accept=".csv,.json,.pdf,.docx" />
    </div>
    <ul class="filelist" id="fileList"></ul>
    <div class="row">
      <div class="field">
        <label for="githubUrl">GitHub Profile (Optional)</label>
        <input type="url" id="githubUrl" placeholder="https://github.com/username" />
        <div class="hint">Example: https://github.com/username</div>
      </div>
      <div class="field">
        <label for="configSelect">Profile Format</label>
        <select id="configSelect">
          <option value="default">Complete Profile</option>
          <option value="custom">Basic Profile</option>
          <option value="__configure__">Configure Output&hellip;</option>
        </select>
        <div class="hint">Same engine, different runtime config &mdash; no code changes.</div>
      </div>
    </div>

    <div class="intake" id="configurePanel" style="display:none; margin-top:20px; padding:22px;">
      <h2>Configure Output</h2>
      <p class="sub">Tick the fields to include, optionally rename them, set normalization, and choose what happens when a value is missing.</p>
      <table id="fieldConfigTable" style="width:100%; border-collapse:collapse;">
        <thead>
          <tr>
            <th style="text-align:left;">Include</th>
            <th style="text-align:left;">Canonical field</th>
            <th style="text-align:left;">Output name (rename)</th>
            <th style="text-align:left;">Normalize</th>
          </tr>
        </thead>
        <tbody id="fieldConfigBody"></tbody>
      </table>
      <div class="row">
        <div class="field">
          <label for="onMissingSelect">On Missing</label>
          <select id="onMissingSelect">
            <option value="null">Set to null</option>
            <option value="omit">Omit field</option>
            <option value="error">Error out</option>
          </select>
          <div class="hint">Applies to every included field that isn't marked required.</div>
        </div>
        <div class="field">
          <label style="display:block; margin-bottom:8px;">
            <input type="checkbox" id="includeConfidence" checked style="width:auto; margin-right:6px;" />
            Include confidence
          </label>
          <label style="display:block;">
            <input type="checkbox" id="includeProvenance" style="width:auto; margin-right:6px;" />
            Include provenance
          </label>
        </div>
      </div>
    </div>

    <div class="row">
      <button class="run" id="runButton">Generate Candidate Profiles</button>
    </div>
    <div class="status-line" id="statusLine"></div>
  </section>
  <section class="results" id="resultsSection" style="display:none;">
    <h2>Candidate Profiles</h2>
    <div class="meta-line" id="metaLine"></div>
    <div id="skippedBox"></div>
    <div id="cardsContainer"></div>
  </section>
  <section class="results" id="emptyState">
    <div class="empty-state">
      <div class="glyph">&empty;</div>
      <div>No dossiers built yet. Add files above and click <strong>Generate Candidate Profiles</strong>.</div>
    </div>
  </section>
  <footer class="page-footer">
    Multi-Source Candidate Data Transformer &mdash; Workflow: Upload &rarr; Extract &rarr; Normalize &rarr; Merge &rarr; Generate Candidate Profile.
  </footer>
</div>
<script>
(function () {
  "use strict";
  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("fileInput");
  var fileListEl = document.getElementById("fileList");
  var githubUrlInput = document.getElementById("githubUrl");
  var configSelect = document.getElementById("configSelect");
  var runButton = document.getElementById("runButton");
  var statusLine = document.getElementById("statusLine");
  var resultsSection = document.getElementById("resultsSection");
  var emptyState = document.getElementById("emptyState");
  var metaLine = document.getElementById("metaLine");
  var skippedBox = document.getElementById("skippedBox");
  var cardsContainer = document.getElementById("cardsContainer");
  var selectedFiles = [];

  // ── Configure Output panel ────────────────────────────────────────────────
  // Mirrors the canonical fields exposed by transformer/projector.py so the
  // user can pick a subset, rename ("path"), set normalization, and choose
  // on_missing / confidence / provenance — all client-side, no code changes.
  var CANONICAL_FIELDS = [
    { from: "candidate_id",        type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "full_name",           type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "emails",              type: "string[]", defaultOn: true,  normalizeOptions: [] },
    { from: "phones",              type: "string[]", defaultOn: true,  normalizeOptions: ["E164"] },
    { from: "location.city",       type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "location.region",     type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "location.country",    type: "string",   defaultOn: true,  normalizeOptions: ["ISO2"] },
    { from: "links.linkedin",      type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "links.github",        type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "links.portfolio",     type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "headline",            type: "string",   defaultOn: true,  normalizeOptions: [] },
    { from: "years_experience",    type: "number",   defaultOn: true,  normalizeOptions: [] },
    { from: "skills[].name",       type: "string[]", defaultOn: true,  normalizeOptions: ["canonical"] },
    { from: "experience",          type: "object",   defaultOn: true,  normalizeOptions: [] },
    { from: "education",           type: "object",   defaultOn: true,  normalizeOptions: [] }
  ];

  function defaultOutputName(fromPath) {
    // "skills[].name" -> "skills", "emails" -> "emails", "location.city" -> "location.city"
    return fromPath.replace(/\\[\\]\\.[A-Za-z_][A-Za-z0-9_]*$/, function (m) {
      return "";
    }).replace(/\\[\\d*\\]/g, "");
  }

  var configurePanel = document.getElementById("configurePanel");
  var fieldConfigBody = document.getElementById("fieldConfigBody");
  var onMissingSelect = document.getElementById("onMissingSelect");
  var includeConfidenceEl = document.getElementById("includeConfidence");
  var includeProvenanceEl = document.getElementById("includeProvenance");

  function buildFieldConfigRows() {
    fieldConfigBody.innerHTML = "";
    CANONICAL_FIELDS.forEach(function (f, idx) {
      var tr = document.createElement("tr");

      var tdCheck = document.createElement("td");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = !!f.defaultOn;
      checkbox.dataset.idx = String(idx);
      checkbox.className = "field-include";
      checkbox.style.width = "auto";
      tdCheck.appendChild(checkbox);

      var tdFrom = document.createElement("td");
      tdFrom.className = "from-path";
      tdFrom.textContent = f.from;

      var tdRename = document.createElement("td");
      var renameInput = document.createElement("input");
      renameInput.type = "text";
      renameInput.value = defaultOutputName(f.from) || f.from;
      renameInput.dataset.idx = String(idx);
      renameInput.className = "field-rename";
      renameInput.style.width = "100%";
      tdRename.appendChild(renameInput);

      var tdNorm = document.createElement("td");
      var normSelect = document.createElement("select");
      normSelect.dataset.idx = String(idx);
      normSelect.className = "field-normalize";
      var noneOpt = document.createElement("option");
      noneOpt.value = "";
      noneOpt.textContent = "\\u2014";
      normSelect.appendChild(noneOpt);
      f.normalizeOptions.forEach(function (n) {
        var opt = document.createElement("option");
        opt.value = n;
        opt.textContent = n;
        normSelect.appendChild(opt);
      });
      tdNorm.appendChild(normSelect);

      tr.appendChild(tdCheck);
      tr.appendChild(tdFrom);
      tr.appendChild(tdRename);
      tr.appendChild(tdNorm);
      fieldConfigBody.appendChild(tr);
    });
  }
  buildFieldConfigRows();

  configSelect.addEventListener("change", function () {
    configurePanel.style.display = configSelect.value === "__configure__" ? "block" : "none";
  });

  function buildCustomConfigFromPanel() {
    var fields = [];
    CANONICAL_FIELDS.forEach(function (f, idx) {
      var checkbox = fieldConfigBody.querySelector('.field-include[data-idx="' + idx + '"]');
      if (!checkbox || !checkbox.checked) return;
      var normSelect = fieldConfigBody.querySelector('.field-normalize[data-idx="' + idx + '"]');
      var entry = {
      path: defaultOutputName(f.from),
      from: f.from,
       type: f.type
    };
      if (f.from === "candidate_id") entry.required = true;
      if (normSelect.value) entry.normalize = normSelect.value;
      fields.push(entry);
    });
    return {
      fields: fields,
      include_confidence: includeConfidenceEl.checked,
      include_provenance: includeProvenanceEl.checked,
      on_missing: onMissingSelect.value
    };
  }

  function renderFileList() {
    fileListEl.innerHTML = "";
    selectedFiles.forEach(function (file, idx) {
      var li = document.createElement("li");
      var label = document.createElement("span");
      label.textContent = file.name;
      var removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "\\u00D7";
      removeBtn.setAttribute("aria-label", "Remove " + file.name);
      removeBtn.addEventListener("click", function () {
        selectedFiles.splice(idx, 1);
        renderFileList();
      });
      li.appendChild(label);
      li.appendChild(removeBtn);
      fileListEl.appendChild(li);
    });
  }
  function addFiles(fileListLike) {
    var incoming = Array.prototype.slice.call(fileListLike);
    var existingNames = {};
    selectedFiles.forEach(function (f) { existingNames[f.name + f.size] = true; });
    incoming.forEach(function (f) {
      var key = f.name + f.size;
      if (!existingNames[key]) { selectedFiles.push(f); existingNames[key] = true; }
    });
    renderFileList();
  }
  dropzone.addEventListener("click", function () { fileInput.click(); });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function (e) { addFiles(e.target.files); });
  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.remove("dragover"); });
  });
  dropzone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });
  function setStatus(text, isError) {
    statusLine.textContent = text || "";
    statusLine.classList.toggle("error", !!isError);
  }
  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }
  function stampFor(confidence) {
    if (confidence == null) return { cls: "unverified", label: "Unscored" };
    if (confidence >= 0.75) return { cls: "verified", label: "High Confidence" };
    if (confidence >= 0.4) return { cls: "partial", label: "Medium Confidence" };
    return { cls: "unverified", label: "Low Confidence" };
  }
  function fieldBlock(label, value) {
    var empty = value == null || value === "" || (Array.isArray(value) && value.length === 0);
    return '<div><div class="fg-label">' + escapeHtml(label) + '</div>' +
      '<div class="fg-value' + (empty ? ' empty' : '') + '">' +
      (empty ? 'not on file' : escapeHtml(value)) + '</div></div>';
  }
  function tagRow(items) {
    if (!items || items.length === 0) return '<div class="fg-value empty">not on file</div>';
    return '<div class="tag-row">' + items.map(function (s) {
      return '<span class="tag">' + escapeHtml(s) + '</span>';
    }).join('') + '</div>';
  }
  function renderCard(candidate) {
    var name = candidate.full_name || 'Unnamed candidate';
    var headline = candidate.headline || '';
    var cid = candidate.candidate_id || '';
    var stamp = stampFor(candidate.overall_confidence);
    var emails = candidate.emails || (candidate.primary_email ? [candidate.primary_email] : []);
    var phones = candidate.phones || (candidate.phone ? [candidate.phone] : []);
    var location = candidate.location || {};
    var locationStr = [location.city, location.region, location.country].filter(Boolean).join(', ');
    var links = candidate.links || {};
    var skills = candidate.skills || [];
    var experience = candidate.experience || [];
    var education = candidate.education || [];
    var provenance = candidate.provenance || [];
    var html = '<div class="card">';
    html += '<div class="card-head">';
    html += '<div><div class="name">' + escapeHtml(name) + '</div>';
    if (headline) html += '<div class="headline">' + escapeHtml(headline) + '</div>';
    if (cid) html += '<div class="candidate-id">Candidate ID: ' + escapeHtml(cid) + '</div>';
    html += '</div>';
    var confLabel = candidate.overall_confidence != null
      ? stamp.label + ' \\u00B7 ' + Math.round(candidate.overall_confidence * 100) + '%'
      : stamp.label;
    html += '<div class="stamp ' + stamp.cls + '">' + escapeHtml(confLabel) + '</div>';
    html += '</div>';
    html += '<div class="card-body">';
    html += '<div class="field-grid">';
    html += fieldBlock('Email', emails.join(', '));
    html += fieldBlock('Phone', phones.join(', '));
    html += fieldBlock('Location', locationStr);
    html += fieldBlock('Years experience', candidate.years_experience);
    html += '</div>';
    if (links.linkedin || links.github || links.portfolio) {
      html += '<div class="field-grid">';
      html += fieldBlock('LinkedIn', links.linkedin);
      html += fieldBlock('GitHub', links.github);
      html += fieldBlock('Portfolio', links.portfolio);
      html += '</div>';
    }
    html += '<div><div class="fg-label" style="margin-bottom:6px;">Skills</div>' + tagRow(skills) + '</div>';
    if (experience.length > 0) {
      html += '<details class="subsection"><summary>Experience (' + experience.length + ')</summary>';
      experience.forEach(function (e) {
        var dates = [e.start, e.end || 'Present'].filter(Boolean).join(' \\u2013 ');
        html += '<div class="exp-entry"><div class="role">' + escapeHtml(e.title || '') +
          (e.company ? ' \\u00B7 ' + escapeHtml(e.company) : '') + '</div>';
        if (dates) html += '<div class="dates">' + escapeHtml(dates) + '</div>';
        if (e.summary) html += '<div>' + escapeHtml(e.summary) + '</div>';
        html += '</div>';
      });
      html += '</details>';
    }
    if (education.length > 0) {
      html += '<details class="subsection"><summary>Education (' + education.length + ')</summary>';
      education.forEach(function (e) {
        html += '<div class="edu-entry"><div class="role">' + escapeHtml(e.degree || '') +
          (e.field ? ' \\u00B7 ' + escapeHtml(e.field) : '') + '</div>';
        html += '<div class="year">' + escapeHtml(e.institution || '') +
          (e.end_year ? ' \\u00B7 ' + escapeHtml(e.end_year) : '') + '</div></div>';
      });
      html += '</details>';
    }
    if (provenance.length > 0) {
      html += '<div class="provenance-log"><div class="pv-title">Data Sources</div>';
      provenance.forEach(function (p) {
        html += '<div class="pv-line"><span class="pv-field">' + escapeHtml(p.field) +
          '</span> \\u2190 <span class="pv-source">' + escapeHtml(p.source) + '</span> (' + escapeHtml(p.method) + ')</div>';
      });
      html += '</div>';
    }
    html += '</div></div>';
    return html;
  }
  function renderResults(data) {
    var candidates = data.candidates || [];
    metaLine.textContent = candidates.length + ' dossier' + (candidates.length === 1 ? '' : 's') +
      ' built \\u00B7 output shape: ' + (data.config_used || 'default');
    skippedBox.innerHTML = '';
    if (data.skipped_files && data.skipped_files.length > 0) {
      var div = document.createElement('div');
      div.className = 'skipped';
      div.textContent = 'Skipped ' + data.skipped_files.length + ' file(s): ' +
        data.skipped_files.map(function (s) { return s.file + ' (' + s.reason + ')'; }).join('; ');
      skippedBox.appendChild(div);
    }
    cardsContainer.innerHTML = candidates.map(renderCard).join('');
    resultsSection.style.display = (candidates.length > 0 || (data.skipped_files || []).length > 0) ? 'block' : 'none';
    emptyState.style.display = candidates.length > 0 ? 'none' : 'block';
  }
  function runTransform() {
    var githubUrl = githubUrlInput.value.trim();
    if (selectedFiles.length === 0 && !githubUrl) {
      setStatus('Add at least one file or a GitHub URL first.', true);
      return;
    }
    if (configSelect.value === '__configure__') {
      var hasAnyField = fieldConfigBody.querySelectorAll('.field-include:checked').length > 0;
      if (!hasAnyField) {
        setStatus('Tick at least one field in Configure Output.', true);
        return;
      }
    }
    runButton.disabled = true;
    setStatus('Generating candidate profiles \\u2014 this can take a few seconds\\u2026');
    resultsSection.style.display = 'none';
    emptyState.style.display = 'none';
    var formData = new FormData();
    selectedFiles.forEach(function (f) { formData.append('files', f, f.name); });
    formData.append('config', configSelect.value);
    if (configSelect.value === '__configure__') {
      formData.append('config_json', JSON.stringify(buildCustomConfigFromPanel()));
    }
    if (githubUrl) formData.append('github_url', githubUrl);
    fetch('/api/transform', { method: 'POST', body: formData })
      .then(function (resp) {
        return resp.json().catch(function () { return null; }).then(function (data) {
          return { resp: resp, data: data };
        });
      })
      .then(function (result) {
        var resp = result.resp, data = result.data;
        if (!resp.ok) {
          var detail = data && data.detail ? data.detail : ('HTTP ' + resp.status);
          setStatus('Failed: ' + detail, true);
          emptyState.style.display = 'block';
          return;
        }
        setStatus('Profiles generated successfully.');
        renderResults(data);
      })
      .catch(function (err) {
        setStatus('Network error: ' + (err && err.message ? err.message : err), true);
        emptyState.style.display = 'block';
      })
      .finally(function () { runButton.disabled = false; });
  }
  runButton.addEventListener('click', runTransform);
})();
</script>
</body>
</html>"""

# ─── Built-in configs ─────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "fields": [
        {"path": "candidate_id", "from": "candidate_id", "type": "string", "required": True},
        {"path": "full_name", "from": "full_name", "type": "string"},
        {"path": "emails", "from": "emails", "type": "string[]"},
        {"path": "phones", "from": "phones", "type": "string[]", "normalize": "E164"},
        {"path": "location.city", "from": "location.city", "type": "string"},
        {"path": "location.region", "from": "location.region", "type": "string"},
        {"path": "location.country", "from": "location.country", "type": "string", "normalize": "ISO2"},
        {"path": "links.linkedin", "from": "links.linkedin", "type": "string"},
        {"path": "links.github", "from": "links.github", "type": "string"},
        {"path": "links.portfolio", "from": "links.portfolio", "type": "string"},
        {"path": "headline", "from": "headline", "type": "string"},
        {"path": "years_experience", "from": "years_experience", "type": "number"},
        {"path": "skills", "from": "skills[].name", "type": "string[]", "normalize": "canonical"},
        {"path": "experience", "from": "experience", "type": "object"},
        {"path": "education", "from": "education", "type": "object"},
    ],
    "include_confidence": True,
    "include_provenance": True,
    "on_missing": "null",
}

_CUSTOM_CONFIG = {
    "fields": [
        {"path": "full_name", "type": "string", "required": True},
        {"path": "primary_email", "from": "emails[0]", "type": "string", "required": True},
        {"path": "phone", "from": "phones[0]", "type": "string", "normalize": "E164"},
        {"path": "skills", "from": "skills[].name", "type": "string[]", "normalize": "canonical"},
    ],
    "include_confidence": True,
    "on_missing": "null",
}

_CONFIGS = {"default": _DEFAULT_CONFIG, "custom": _CUSTOM_CONFIG}

# Sentinel value sent by the frontend's "Configure Output…" option. When the
# client picks this, the actual config travels separately in `config_json`.
_CONFIGURE_SENTINEL = "__configure__"

_ALLOWED_EXTENSIONS = {".csv", ".json", ".pdf", ".docx"}

# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="Candidate Data Transformer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def root() -> HTMLResponse:
    """Serve the UI from the embedded string — zero filesystem access."""
    return HTMLResponse(content=_FRONTEND_HTML)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api")
def health() -> dict:
    return {"status": "ok", "service": "candidate-data-transformer"}


@app.get("/api/configs")
def list_configs() -> dict:
    return {"configs": list(_CONFIGS.keys()), "configurable": _CONFIGURE_SENTINEL}


@app.post("/api/transform")
async def transform(
    files: list[UploadFile] = File(...),
    config: str = Form("default"),
    config_json: Optional[str] = Form(None),
    github_url: Optional[str] = Form(None),
) -> JSONResponse:
    # ── Resolve which output config to use ──────────────────────────────────
    if config == _CONFIGURE_SENTINEL:
        # User-built config from the "Configure Output…" panel.
        if not config_json or not config_json.strip():
            raise HTTPException(
                status_code=400,
                detail="config_json is required when config='__configure__'.",
            )
        try:
            chosen_config = json.loads(config_json)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid config_json: {e}")

        problems = validate_config(chosen_config)
        if problems:
            # This is user-supplied input, so a bad shape is a 400, not a 500.
            raise HTTPException(status_code=400, detail=f"Invalid output config: {problems}")
    elif config in _CONFIGS:
        chosen_config = _CONFIGS[config]
        problems = validate_config(chosen_config)
        if problems:
            # Our own built-in presets failing validation is a server bug.
            raise HTTPException(status_code=500, detail=f"Internal config error: {problems}")
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown config '{config}'. Choose: {list(_CONFIGS)} or '{_CONFIGURE_SENTINEL}'.",
        )

    if not files and not github_url:
        raise HTTPException(status_code=400, detail="Upload at least one file or provide a GitHub URL.")

    source_inputs: list[SourceInput] = []
    skipped: list[dict] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        for upload in files:
            filename = upload.filename or "upload"
            suffix = Path(filename).suffix.lower()

            if suffix not in _ALLOWED_EXTENSIONS:
                skipped.append({"file": filename, "reason": f"unsupported file type '{suffix}'"})
                continue

            kind = detect_source_kind(filename)
            if kind is None:
                skipped.append({"file": filename, "reason": "could not detect source type"})
                continue

            dest = tmp_path / filename
            content = await upload.read()
            dest.write_bytes(content)
            source_inputs.append(SourceInput(str(dest), kind=kind))
            logger.info(f"Accepted '{filename}' as '{kind}'")

        if github_url and github_url.strip():
            source_inputs.append(SourceInput(github_url.strip(), kind="github"))

        if not source_inputs:
            raise HTTPException(
                status_code=400,
                detail=f"No usable sources. Skipped: {skipped}" if skipped else "No usable sources provided.",
            )

        started = time.monotonic()
        try:
            results = run_pipeline(source_inputs, chosen_config)
        except ProjectionError as e:
            raise HTTPException(status_code=400, detail=str(e))
        elapsed = time.monotonic() - started
        logger.info(f"Pipeline done in {elapsed:.2f}s, {len(results)} candidate(s)")

    return JSONResponse({
        "candidates": results,
        "count": len(results),
        "skipped_files": skipped,
        "config_used": config,
    })