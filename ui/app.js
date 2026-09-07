/*
 * DDPSRUN-UI-APP
 *
 * The whole screen. It makes no decisions of its own.
 *
 * END-TO-END FLOW:
 *   1. Read the server address and token out of localStorage. With neither,
 *      only #login is shown.
 *   2. The address bar's hash decides which screen is up (`#/jobs`,
 *      `#/jobs/job-abc...`). A hash rather than a path because S3 static
 *      hosting has no server-side routing: reloading on `/jobs` would 404.
 *   3. Every screen change stops the previous screen's polling timers and
 *      starts its own (`poll.stop()` / `poll.every()`).
 *   4. The draw functions put the server's values on screen unchanged. The
 *      time estimate, the recommended GPU and the validation findings are all
 *      sentences the lambda wrote.
 *
 * Why no decisions here: if the CLI and the screen ever disagree there is no
 * way to tell which one is right. So the judgement lives in exactly one place.
 *
 * Why polling: a lambda invocation is capped at 15 minutes and training runs
 * for tens of hours, so a held-open connection is impossible. The screen asks
 * again every few seconds and remembers the last log timestamp it saw, which
 * is what keeps the server stateless.
 *
 * The intervals and what they cost are worked out in docs/15-screens.md 15.7
 * (one detail screen held open for an hour is about $0.005).
 *
 * Status words are printed verbatim — Running, Compared, Failed — because they
 * are the same strings `kubectl get pacsjobs` prints and the user has to be
 * able to match one against the other.
 */

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ storage */

const store = {
  get server() { return localStorage.getItem("ddpsrun.server") || ""; },
  get token() { return localStorage.getItem("ddpsrun.token") || ""; },
  set(server, token) {
    localStorage.setItem("ddpsrun.server", server.replace(/\/+$/, ""));
    localStorage.setItem("ddpsrun.token", token);
  },
  clear() {
    localStorage.removeItem("ddpsrun.server");
    localStorage.removeItem("ddpsrun.token");
  },
};

/* The only way this page reaches the lambda. Error text is shown exactly as the
   server wrote it: the CRD's validation messages are written for a person to
   read, so rewriting them here would only lose information. */
async function call(path, options = {}) {
  // Before every request, not on a timer: a tab left open overnight would sleep
  // through a timer, and the next thing the user does is the moment that
  // matters. A static token has no expiry and this returns immediately.
  await refreshIfExpired();
  const response = await fetch(store.server + path, {
    ...options,
    headers: {
      "Authorization": "Bearer " + store.token,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* may not be JSON */ }
  if (!response.ok) {
    const detail = body && body.detail;
    throw new Error(
      typeof detail === "string" ? detail
      : Array.isArray(detail)
        ? detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
        // No `detail` means the body was not this API's JSON at all, which most
      // often means the request reached something else entirely. Naming where
      // it went matters: a bare status code sends people to the wrong machine.
      : `The server answered ${response.status} for ${store.server}${path}`
    );
  }
  return body;
}

/* ------------------------------------------------------------------ helpers */

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* PacsJob's seven phases (`api/v1alpha1/pacsjob_types.go:64-69` and `:85`).
   Three of them end the job and never change again. Compared is the one that
   is easy to miss: a mode=compare job priced every candidate offering and
   deliberately bought nothing, so it is finished and it is not a failure. */
const TERMINAL = ["Succeeded", "Failed", "Compared"];

/* Map a phase onto a badge colour. The phase name itself is the label — never a
   translation of it — because colour alone carries nothing to a reader who
   cannot distinguish it (docs/15-screens.md 15.9), and because the same word
   has to appear in `kubectl` output and in this badge. */
function statusOf(phase) {
  switch (phase) {
    case "Succeeded":  return { cls: "ok",   text: "Succeeded" };
    case "Compared":   return { cls: "ok",   text: "Compared" };
    case "Failed":     return { cls: "bad",  text: "Failed" };
    case "Running":    return { cls: "run",  text: "Running" };
    case "Starting":   return { cls: "run",  text: "Starting" };
    case "Recovering": return { cls: "run",  text: "Recovering" };
    case "Pending":    return { cls: "wait", text: "Pending" };
    default:           return { cls: "wait", text: phase || "Unknown" };
  }
}
const badge = (phase) => {
  const s = statusOf(phase);
  return `<span class="badge ${s.cls}">${esc(s.text)}</span>`;
};

/* Write the gap between two RFC 3339 stamps as "2h 30m". An unfinished job
   measures to now. With no start stamp this returns null and the caller decides
   what to print, because "how long has it run" and "how long has it waited" are
   different questions and must not share a number. */
function span(fromISO, toISO) {
  if (!fromISO) return null;
  const from = Date.parse(fromISO);
  const to = toISO ? Date.parse(toISO) : Date.now();
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  const sec = Math.max(0, Math.round((to - from) / 1000));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec % 60}s`;
  return `${sec}s`;
}

/* The jobs table's Elapsed column. Three cases, and telling them apart is the
 * whole point — merging any two makes the number a lie.
 *
 *   1. startedAt is set        -> how long it actually ran.
 *   2. not finished, no start  -> how long it has been queued. Say "queued".
 *   3. finished, no start      -> unknowable. Print "-".
 *
 * Case 3 is real. The controller that stamps startedAt/finishedAt
 * (PACSRUN-JOB-CLOCK) started 2026-09-01T00:06:53Z and stamps once without
 * backfilling, while the newest PacsJob on the cluster was created
 * 2026-08-29T15:23:25Z. Without this branch, aiops-exp2 — which succeeded days
 * ago — read as "64h 42m queued". On 2026-09-01 all 24 jobs were this case.
 */
function elapsedCell(job) {
  const run = span(job.started_at, job.finished_at);
  if (run) return `<span class="num">${run}</span>`;
  if (TERMINAL.includes(job.phase)) {
    return `<span class="dim" title="This job finished before the timestamps were recorded">-</span>`;
  }
  const wait = span(job.created_at, null);
  return wait ? `<span class="num dim">${wait} queued</span>` : `<span class="dim">-</span>`;
}

const when = (iso) => iso ? iso.replace("T", " ").replace("Z", "").slice(5, 16) : "-";

function note(kind, text, fix) {
  return `<div class="note ${kind}"><div>${esc(text)}` +
    (fix ? `<div class="fix">${esc(fix)}</div>` : "") + `</div></div>`;
}

function empty(text, buttonLabel, gotoView) {
  return `<div class="empty"><p>${esc(text)}</p>` +
    (buttonLabel ? `<button class="go" data-goto="${gotoView}">${esc(buttonLabel)}</button>` : "") +
    `</div>`;
}

/* Timers that live only while one screen is up. Leaving a screen must stop
   them; otherwise the jobs list keeps calling the lambda every 15 seconds long
   after the user has moved on, and every one of those calls is billed. */
const poll = {
  timers: [],
  every(seconds, fn) {
    fn();
    this.timers.push(setInterval(fn, seconds * 1000));
  },
  stop() {
    this.timers.forEach(clearInterval);
    this.timers = [];
  },
};

/* ------------------------------------------------------------------ routing */

const VIEWS = ["home", "jobs", "detail", "submit", "team"];

function show(view) {
  VIEWS.forEach((v) => { $("view-" + v).hidden = v !== view; });
  document.querySelectorAll("nav button[data-view]").forEach((b) => {
    // The detail screen is reached from the list, so the nav keeps Jobs lit.
    b.classList.toggle("on", b.dataset.view === (view === "detail" ? "jobs" : view));
  });
}

/* The hash is the single source of truth. Buttons only change the hash; the
   drawing happens here, in one place, so a reload lands on the same screen. */
async function route() {
  poll.stop();
  const hash = location.hash.replace(/^#\/?/, "");
  const [head, arg] = hash.split("/");

  try {
    if (head === "jobs" && arg) {
      // "<id>@<ns>": an operator viewing a foreign namespace carries it in the
      // hash, so a reload of this page still asks the right namespace. "@" can
      // appear in neither part — ids are hex, namespaces are DNS labels — so
      // the split is unambiguous.
      const [jobId, jobNs] = arg.split("@");
      show("detail"); await drawDetail(jobId, jobNs || "");
    }
    else if (head === "jobs")   { show("jobs");   drawJobs(); }
    else if (head === "submit") { show("submit"); }
    else if (head === "team")   { show("team");   drawTeam(); }
    else                        { show("home");   drawHome(); }
  } catch (err) {
    console.error(err);
  }
}

function go(view, arg) { location.hash = "#/" + view + (arg ? "/" + arg : ""); }

/* ------------------------------------------------------------------ 1. Home */

function drawHome() {
  poll.every(30, async () => {
    let jobs = [], stats = null;
    try {
      const [list, s] = await Promise.all([
        call("/v1/jobs?limit=1000"),
        call("/v1/stats").catch(() => null),
      ]);
      jobs = list.jobs || [];
      stats = s;
    } catch (err) {
      $("home-cards").innerHTML = note("err", err.message);
      return;
    }

    const today = new Date().toISOString().slice(0, 10);
    const isToday = (j) => (j.finished_at || j.created_at || "").startsWith(today);
    const running = jobs.filter((j) => !TERMINAL.includes(j.phase));
    const doneToday = jobs.filter((j) => j.phase === "Succeeded" && isToday(j)).length;
    const failToday = jobs.filter((j) => j.phase === "Failed" && isToday(j)).length;

    $("home-cards").innerHTML = [
      card("Active", running.length, running.length ? "run" : ""),
      card("Finished today", doneToday, doneToday ? "ok" : ""),
      card("Failed today", failToday, failToday ? "bad" : ""),
      card("Team spend", stats ? "$" + stats.cost_usd.toFixed(2) : "-"),
    ].join("");

    $("home-running-note").textContent = running.length ? `${running.length} running` : "";
    $("home-running").innerHTML = running.length
      ? jobsTable(running.slice(0, 5), ["name", "status", "elapsed", "gpu"])
      : empty("Nothing is running right now.", "New job", "submit");
    wireRows($("home-running"));
  });
}

const card = (label, value, cls = "") =>
  `<div class="card"><span class="label">${esc(label)}</span>` +
  `<span class="value ${cls}">${esc(value)}</span></div>`;

/* ------------------------------------------------------------------ 2. Jobs */

let jobsTab = "active";

/* DDPSRUN-UI-NAMESPACE. Which namespace the jobs screen reads. `current` empty
   means the caller's own, which is all a non-operator ever sees: the picker is
   drawn only when GET /v1/namespaces answers selectable (the token file's admin
   flag) with more than one namespace. A chosen foreign namespace rides into the
   detail hash as "#/jobs/<id>@<ns>", so reloading a foreign job's page still
   asks the right namespace. The server enforces all of this with 403 anyway —
   this state only decides what the screen offers. */
let nsView = { loaded: false, list: [], own: "", selectable: false, current: "" };

async function drawJobs() {
  document.querySelectorAll("#jobs-tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.phase === jobsTab);
  });

  // Ask once per sign-in which namespaces this caller may read. Failing quiet
  // is deliberate: against an older server without the route, the picker just
  // stays hidden and the screen is exactly what it was before namespaces.
  if (!nsView.loaded) {
    try {
      const r = await call("/v1/namespaces");
      nsView = { loaded: true, list: r.namespaces || [], own: r.own || "",
                 selectable: Boolean(r.selectable), current: "" };
    } catch { nsView.loaded = true; }
    const sel = $("jobs-ns");
    sel.hidden = !(nsView.selectable && nsView.list.length > 1);
    if (!sel.hidden) {
      sel.innerHTML = nsView.list.map((n) =>
        `<option value="${esc(n)}"${n === nsView.own ? " selected" : ""}>${esc(n)}</option>`
      ).join("");
    }
  }

  poll.every(15, async () => {
    let result;
    try {
      const nsq = nsView.current ? "&namespace=" + encodeURIComponent(nsView.current) : "";
      result = await call(`/v1/jobs?limit=200${jobsTab ? "&phase=" + jobsTab : ""}${nsq}`);
    } catch (err) {
      $("jobs-body").innerHTML = note("err", err.message);
      return;
    }
    const jobs = result.jobs || [];

    // Say so when the list was cut. Hiding the difference silently would make
    // the screen claim a completeness it does not have.
    $("jobs-count").textContent = result.total > jobs.length
      ? `showing ${jobs.length} of ${result.total}`
      : `${result.total} ${result.total === 1 ? "job" : "jobs"}`;

    $("jobs-body").innerHTML = jobs.length
      ? jobsTable(jobs, ["name", "id", "user", "status", "created", "elapsed", "gpu", "vendor", "cost", "recovery", "result"])
      : empty(
          jobsTab === "active" ? "Nothing is running right now."
          : jobsTab === "finished" ? "No job has finished yet."
          : "You have not submitted a job yet.",
          "New job", "submit");
    wireRows($("jobs-body"), nsView.current);
  });
}

// Changing the picker re-reads the list. poll.stop() first, because poll.every
// only ever adds timers — without it the old namespace would keep refreshing
// the table underneath the new one every 15 seconds.
$("jobs-ns").onchange = () => {
  const picked = $("jobs-ns").value;
  nsView.current = picked === nsView.own ? "" : picked;
  poll.stop();
  drawJobs();
};

/* Build one table. The caller picks the columns: Home uses 4, the jobs screen
   uses 11 (the 9 in docs/15-screens.md 15.5, plus Cost and Result). */
function jobsTable(jobs, columns) {
  const HEAD = {
    name: "Name", id: "ID", user: "Submitted by", status: "Status", created: "Created",
    elapsed: "Elapsed", gpu: "GPU", vendor: "Vendor", cost: "Cost",
    recovery: "Restarts", result: "Result",
  };
  const CELL = {
    name: (j) => `<span class="name">${esc(j.name || "(unnamed)")}</span>`,
    // Jobs with no id do exist: a PacsJob applied with kubectl does not follow
    // the ddpsrun-<hex> naming rule, so no id can be read off it — on
    // 2026-09-01 that was all 24 jobs on the cluster. This cell says where the
    // job came from; the row still clicks through, because the server also
    // accepts the object NAME as the detail key (DDPSRUN-JOB-BY-NAME).
    id: (j) => j.job_id
      ? `<span class="num dim tiny">${esc(j.job_id)}</span>`
      : `<span class="dim tiny" title="Created outside this gateway">applied directly</span>`,
    // The owner label is written by the server at submit time and by nothing
    // else, so a job with no owner was applied straight to the cluster with
    // kubectl — and only an operator can do that. Naming the operator tells
    // the reader who to ask about the job; "-" told them nothing.
    user: (j) => esc(j.user || "admin"),
    status: (j) => badge(j.phase),
    created: (j) => `<span class="num dim">${esc(when(j.created_at))}</span>`,
    elapsed: elapsedCell,
    gpu: (j) => `<span class="num">${esc(j.gpu || "-")}</span>`,
    vendor: (j) => esc(j.vendor || "-"),
    recovery: (j) => j.recovery_count
      ? `<span class="num" style="color:var(--run)">${j.recovery_count}</span>`
      : `<span class="dim">-</span>`,
    // Dollars from the server (JobView.cost_usd), never computed here: it is
    // the same number /v1/stats adds into the team total. null means "no
    // price known" — a machine we never measured, or a job that never ran —
    // and "-" is the honest rendering of that, where $0.00 would be a lie.
    cost: (j) => (j.cost_usd == null)
      ? `<span class="dim">-</span>`
      : `<span class="num">$${Number(j.cost_usd).toFixed(2)}</span>`,
    // resultPath is a spec field: it names where output WILL land, from the
    // moment the job exists. So this cell shows the destination folder, not a
    // claim that anything was saved — a Running job has a path and no file
    // yet. The visible part is the job's own folder name (the piece a person
    // can recognise in `aws s3 ls`); the full URI is in the tooltip and on
    // the detail screen.
    result: (j) => {
      if (!j.result_path) return `<span class="dim">-</span>`;
      const tail = j.result_path.replace(/\/+$/, "").split("/").pop();
      return `<span class="num dim tiny" title="${esc(j.result_path)}">${esc(tail)}</span>`;
    },
  };

  return `<div class="scroll"><table><thead><tr>` +
    columns.map((c) => `<th>${HEAD[c]}</th>`).join("") +
    `</tr></thead><tbody>` +
    jobs.map((j) => {
      // The click-through key: the gateway's id when there is one, else the
      // object name itself — the second spelling the server accepts
      // (DDPSRUN-JOB-BY-NAME). For a kubectl-applied job the display name IS
      // metadata.name (models.py falls back to it), so it is the right key.
      const key = j.job_id || j.name;
      return `<tr class="${key ? "click" : ""}${j.phase === "Failed" ? " failed" : ""}" ` +
        `data-id="${esc(key)}">` +
        columns.map((c) => `<td${c === "elapsed" || c === "created" ? ' class="num"' : ""}>${CELL[c](j)}</td>`).join("") +
        `</tr>`;
    }).join("") +
    `</tbody></table></div>`;
}

function wireRows(root, ns) {
  root.querySelectorAll("tr.click").forEach((tr) => {
    // ns is set only when an operator is looking at a foreign namespace; it
    // rides in the hash so the detail screen (and a reload of it) asks the
    // same namespace the row came from.
    tr.onclick = () => go("jobs", tr.dataset.id + (ns ? "@" + ns : ""));
  });
}

/* ------------------------------------------------------------------ 3. Detail */

let logSeen = null;   // last timestamp seen. This is what keeps the server stateless.
let logText = "";
let lastSpec = null;  // what "Run again" copies from.
let detailNs = "";    // which namespace the open detail screen reads. "" = own.

/* The ?namespace= suffix every detail request carries when an operator opened
   a foreign job. One helper rather than five string concatenations, because
   the logs URL sometimes already has ?since= and needs "&" instead of "?". */
const nsQuery = (sep = "?") =>
  detailNs ? `${sep}namespace=${encodeURIComponent(detailNs)}` : "";

async function drawDetail(jobId, ns = "") {
  detailNs = ns;
  logSeen = null;
  logText = "";
  lastSpec = null;
  $("d-log").textContent = "Waiting for output.";
  $("d-id").textContent = jobId;

  // Once per open, not per poll — see the panel's comment in index.html.
  drawArtifacts(jobId);
  $("d-files-refresh").onclick = () => drawArtifacts(jobId);

  // The spec never changes, so read it once rather than on every poll.
  call(`/v1/jobs/${jobId}/spec` + nsQuery()).then((spec) => {
    lastSpec = spec;
    $("d-spec").textContent = JSON.stringify(spec.spec, null, 2);
    $("d-spec-note").innerHTML = spec.redacted.length
      ? note("info", `${spec.redacted.join(", ")} came from a Kubernetes Secret. ` +
             `Neither the value nor the Secret's name ever reaches this page.`)
      : "";
  }).catch((err) => { $("d-spec").textContent = err.message; });

  poll.every(5, async () => {
    let job;
    try {
      job = await call(`/v1/jobs/${jobId}` + nsQuery());
    } catch (err) {
      $("d-message").innerHTML = note("err", err.message);
      poll.stop();
      return;
    }

    const s = statusOf(job.phase);
    $("d-name").textContent = job.name || jobId;
    $("d-badge").innerHTML = badge(job.phase);
    $("d-message").innerHTML = job.message
      ? note(s.cls === "bad" ? "err" : "info", job.message)
      : "";

    // The same three cases as elapsedCell. A finished job with no timestamps
    // says so rather than counting up from its creation time.
    const done = TERMINAL.includes(job.phase);
    const waitText = job.started_at ? (span(job.created_at, job.started_at) || "-")
      : done ? "not recorded"
      : (span(job.created_at, null) || "-") + " so far";
    $("d-facts").innerHTML = [
      fact("Created", when(job.created_at)),
      fact("Queued", waitText),
      fact("Ran for", span(job.started_at, job.finished_at) || (done ? "not recorded" : "-")),
      fact("GPU", job.gpu || "not yet known"),
      fact("Vendor", job.vendor || "not yet known"),
      fact("Restarts", job.recovery_count || "none"),
      // Same fallback as the list: no owner label means it was applied with
      // kubectl, which only an operator can do.
      fact("Submitted by", job.user || "admin"),
      fact("Result", job.result_path || "-"),
    ].join("");

    await Promise.all([drawMetrics(jobId, job), drawLog(jobId)]);
    // A finished job has nothing left to ask about. Stopping the timers here is
    // also where the billing for this screen stops.
    if (done) poll.stop();
  });
}

const fact = (k, v) =>
  `<div class="fact"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`;

/* The Result files panel: GET /v1/jobs/{id}/artifacts, drawn as a table with
   one Download link per file. The link is a presigned S3 URL — the browser
   follows it to S3 directly, so the bytes never pass through Lambda (whose
   response is capped around 6 MB; one adapter file measured 528,550,256
   bytes). Links expire after 10 minutes; Refresh mints fresh ones. */
async function drawArtifacts(jobId) {
  $("d-files-note").textContent = "";
  $("d-files").innerHTML = `<p class="dim">Loading...</p>`;
  let a;
  try {
    a = await call(`/v1/jobs/${jobId}/artifacts` + nsQuery());
  } catch (err) {
    $("d-files").innerHTML = note("err", err.message);
    return;
  }
  if (!a.files.length) {
    $("d-files").innerHTML = `<p class="dim">${esc(a.note || "No files.")}</p>`;
    return;
  }
  $("d-files-note").textContent =
    `${a.total} ${a.total === 1 ? "file" : "files"}` +
    `${a.truncated ? " (first 1000 only)" : ""}, links are good for 10 minutes`;
  $("d-files").innerHTML =
    `<div class="scroll"><table><thead><tr>` +
    `<th>File</th><th>Size</th><th>Last written</th><th></th>` +
    `</tr></thead><tbody>` +
    a.files.map((f) =>
      `<tr><td><span class="name">${esc(f.name)}</span></td>` +
      `<td class="num">${esc(humanSize(f.size_bytes))}</td>` +
      `<td class="num dim">${esc(when(f.last_modified))}</td>` +
      `<td><a class="flat tiny" style="padding:4px 10px" href="${esc(f.url)}" ` +
      `target="_blank" rel="noopener">Download</a></td></tr>`
    ).join("") +
    `</tbody></table></div>`;
}

/* "528550256" is unreadable at a glance; "504.1 MiB" is what a person needs to
   decide whether to download on the network they are on. Powers of 1024, the
   same convention `ls -lh` and the S3 console print. */
function humanSize(n) {
  if (!Number.isFinite(n) || n < 1024) return `${n} B`;
  let v = n;
  for (const unit of ["KiB", "MiB", "GiB", "TiB"]) {
    v /= 1024;
    if (v < 1024) return `${v.toFixed(1)} ${unit}`;
  }
  return `${v.toFixed(1)} PiB`;
}

async function drawMetrics(jobId, job) {
  // The server's window is measured back from NOW, and a finished job's
  // readings sit at the END of its life — possibly days ago. So for a
  // terminal job, ask for a window that reaches back past its startedAt
  // (plus an hour of slack); a running job keeps the default hour. Capped at
  // the server's seven days: a pod older than that is usually collected, and
  // the durable home for old readings is Prometheus, not this log.
  let query = nsQuery();
  if (job && TERMINAL.includes(job.phase) && job.started_at) {
    const back = Math.ceil((Date.now() - Date.parse(job.started_at)) / 1000) + 3600;
    query = `?window_seconds=${Math.min(Math.max(back, 60), 604800)}` + nsQuery("&");
  }
  let m;
  try { m = await call(`/v1/jobs/${jobId}/metrics` + query); }
  catch { return; }   // 404 while the pod does not exist yet. Normal; stay quiet.

  const p = m.progress;
  $("d-progress-panel").hidden = !p;
  if (p) {
    $("d-progress-note").textContent = p.steady
      ? `${p.seconds_per_step.toFixed(2)}s per step`
      : "the rate has not settled yet, so the remaining time will move around";
    $("d-progress").innerHTML =
      `<div class="bar${p.percent >= 100 ? " done" : ""}"><i style="width:${Math.min(100, p.percent)}%"></i></div>` +
      `<div class="facts" style="margin-top:16px">` +
      fact("Progress", `${p.percent.toFixed(1)}% (${p.step}/${p.total_steps} steps)`) +
      fact("Elapsed", p.elapsed) +
      fact("Remaining", p.remaining) +
      fact("Projected total", p.projected_total_hours.toFixed(2) + " h") +
      `</div>`;
  }

  const series = m.gpu_series || [];
  $("d-gpu-panel").hidden = !series.length;
  if (series.length) {
    const last = m.latest_gpu;
    $("d-gpu-note").textContent = `last ${m.window_seconds}s, ${series.length} samples`;
    $("d-gpu").innerHTML =
      `<div class="facts">` +
      fact("Utilisation", last.utilization_percent + "%") +
      fact("Memory", `${last.memory_used_mib} / ${last.memory_total_mib} MiB (${last.memory_percent.toFixed(0)}%)`) +
      fact("Temperature", last.temperature_c + " °C") +
      fact("Power", last.power_w.toFixed(0) + " W") +
      `</div>` +
      sparkline(series) +
      `<div class="legend"><span><i style="background:var(--accent)"></i>utilisation</span>` +
      `<span><i style="background:var(--run)"></i>memory</span></div>`;
  }
}

/* Two polylines drawn as SVG. No charting library, for two reasons: the page is
   served as static files under a CSP that blocks external hosts, so a CDN is
   not available; and one more file to ship is a poor trade for a plot this
   small. Both values are already 0-100, so no axis scaling is needed. */
function sparkline(series) {
  const W = 600, H = 140, pad = 4;
  const path = (pick, color) => {
    const points = series.map((s, i) => {
      const x = pad + (i / Math.max(1, series.length - 1)) * (W - pad * 2);
      const y = H - pad - (Math.max(0, Math.min(100, pick(s))) / 100) * (H - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.6" ` +
           `stroke-linejoin="round" stroke-linecap="round"/>`;
  };
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" ` +
    `aria-label="GPU utilisation and memory over time">` +
    [25, 50, 75].map((v) =>
      `<line x1="0" y1="${H - pad - (v / 100) * (H - pad * 2)}" x2="${W}" ` +
      `y2="${H - pad - (v / 100) * (H - pad * 2)}" stroke="var(--line)" stroke-width="1"/>`).join("") +
    path((s) => s.utilization_percent, "var(--accent)") +
    path((s) => s.memory_percent, "var(--run)") +
    `</svg>`;
}

async function drawLog(jobId) {
  let r;
  try {
    // The first read after opening asks for NO time window (0): just the last
    // 500 lines of the whole log. Every later read is incremental from the
    // last timestamp seen. Without the backfill, a job that has been running
    // for hours — or finished days ago — showed "No output yet." while a
    // 30-second live window stayed empty (baseline-c, 2026-09-07: its scoring
    // phase printed nothing for 20+ minutes).
    r = await call(`/v1/jobs/${jobId}/logs` +
      (logSeen ? `?since=${encodeURIComponent(logSeen)}` + nsQuery("&")
               : `?window_seconds=0&max_lines=500` + nsQuery("&")));
  } catch { return; }

  const lines = r.lines || [];
  if (lines.length) {
    logText += (logText ? "\n" : "") + lines.join("\n");
    $("d-log").textContent = logText;
    $("d-log").scrollTop = $("d-log").scrollHeight;
  } else if (!logText) {
    $("d-log").textContent = "No output yet.";
  }
  if (r.last_timestamp) logSeen = r.last_timestamp;
  const n = logText.split("\n").filter(Boolean).length;
  $("d-log-note").textContent = `${n} ${n === 1 ? "line" : "lines"}`;
}

/* ------------------------------------------------------------------ 4. Submit */

let draft = null;   // built in step 1; steps 2 and 3 send the same object again.

function readForm() {
  const env = {};
  ($("f-env").value || "").split("\n").forEach((line) => {
    const t = line.trim();
    if (!t) return;
    const i = t.indexOf("=");
    if (i > 0) env[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  });

  const num = (id) => {
    const v = parseInt($(id).value, 10);
    return Number.isFinite(v) ? v : null;
  };

  const body = {
    name: $("f-name").value.trim() || "untitled",
    image: $("f-image").value.trim(),
    command: $("f-command").value.trim() || null,
    parallelism: num("f-parallelism") || 1,
    capacity_type: $("f-capacity").value,
    env,
    training: {},
  };
  if ($("f-gpu").value) body.gpu = { name: $("f-gpu").value, count: 1 };
  if ($("f-result").value.trim()) body.result_path = $("f-result").value.trim();

  const t = body.training;
  if (num("f-pairs")) t.pairs = num("f-pairs");
  if (num("f-epochs")) t.epochs = num("f-epochs");
  if (num("f-cap")) t.cap = num("f-cap");
  if (num("f-batch")) t.batch_size = num("f-batch");

  return body;
}

function step(n) {
  $("s1").hidden = n !== 1;
  $("s2").hidden = n !== 2;
  $("s3").hidden = n !== 3;
  $("s-step").textContent = n;
  $("s-what").textContent = ["", "Describe", "Validate", "Cost"][n];
}

$("s1-next").onclick = async () => {
  $("s1-err").innerHTML = "";
  draft = readForm();
  if (!draft.image) {
    $("s1-err").innerHTML = note("err", "An image is required.",
      "Without one the cluster has nothing to start: the image is the container the job runs in.");
    return;
  }
  let v;
  try { v = await call("/v1/validate", { method: "POST", body: JSON.stringify(draft) }); }
  catch (err) { $("s1-err").innerHTML = note("err", err.message); return; }

  const findings = v.findings || [];
  const errors = findings.filter((f) => f.level === "error");
  $("s2-findings").innerHTML = findings.length
    ? findings.map((f) =>
        note(f.level === "error" ? "err" : f.level === "warning" ? "warn" : "info",
             f.message, f.fix)).join("")
    : note("info", "Nothing to flag.");
  // With an error present the next button does not work (15.10), and it says
  // what has to happen instead of just going grey.
  $("s2-next").disabled = errors.length > 0;
  $("s2-next").textContent = errors.length
    ? `Fix ${errors.length} ${errors.length === 1 ? "error" : "errors"} first`
    : "See the cost";
  step(2);
};

$("s1-reset").onclick = () => {
  ["f-name", "f-image", "f-command", "f-result", "f-env",
   "f-pairs", "f-epochs", "f-cap", "f-batch"].forEach((id) => { $(id).value = ""; });
  $("f-parallelism").value = 1;
  $("f-gpu").value = "";
  $("f-capacity").value = "spot";
  $("s1-err").innerHTML = "";
};

$("s2-back").onclick = () => step(1);
$("s3-back").onclick = () => step(2);

$("s2-next").onclick = async () => {
  let e;
  try { e = await call("/v1/estimate", { method: "POST", body: JSON.stringify(draft) }); }
  catch (err) { $("s2-findings").innerHTML += note("err", err.message); return; }

  const money = (r) => (r.low == null || r.high == null) ? "unknown"
    : r.low === r.high ? "$" + r.low.toFixed(2)
    : `$${r.low.toFixed(2)} - $${r.high.toFixed(2)}`;
  const hours = (r) => (r.low == null || r.high == null) ? "unknown"
    : r.low === r.high ? r.low.toFixed(1) + " h"
    : `${r.low.toFixed(1)} - ${r.high.toFixed(1)} h`;

  $("s3-basis").textContent = e.basis;
  $("s3-cards").innerHTML = [
    card("Estimated cost", money(e.cost_usd)),
    card("Estimated time", hours(e.hours)),
    card("Steps", e.steps ?? "unknown"),
    card("Capacity type", e.capacity_type),
  ].join("");

  const notes = [];
  // With confidence "unknown" the numbers above rest on nothing. Say so loudly.
  if (e.hours.confidence === "unknown") {
    notes.push(note("warn", "This estimate has no measured run behind it.",
      "Treat the numbers as a guess. A short trial run and a second estimate is the cheaper path."));
  } else {
    notes.push(note("info",
      `Basis: ${e.hours.confidence === "measured" ? "a measured run" : "interpolation between measured runs"}`));
  }
  if (e.gpu.recommended) {
    notes.push(note("info",
      `Recommended GPU: ${e.gpu.recommended} (logits peak at ${e.gpu.peak_logits_gib.toFixed(2)} GiB). ${e.gpu.reason}`));
  }
  notes.push(note("info", `Capacity type read as ${e.capacity_type}: ${e.capacity_reason}`));
  (e.warnings || []).forEach((w) => notes.push(note("warn", w)));
  $("s3-warnings").innerHTML = notes.join("");

  step(3);
};

$("s3-submit").onclick = async () => {
  $("s3-err").innerHTML = "";
  $("s3-submit").disabled = true;
  try {
    const r = await call("/v1/jobs", { method: "POST", body: JSON.stringify(draft) });
    step(1);
    $("s1-reset").click();
    go("jobs", r.job_id);
  } catch (err) {
    $("s3-err").innerHTML = note("err", err.message);
  } finally {
    $("s3-submit").disabled = false;
  }
};

/* ------------------------------------------------------------------ 5. Team */

async function drawTeam() {
  let s;
  try { s = await call("/v1/stats"); }
  catch (err) { $("team-body").innerHTML = note("err", err.message); return; }

  $("team-note").textContent = s.team ? `team ${s.team}` : "";
  $("team-cards").innerHTML = [
    card("Jobs", s.jobs),
    card("GPU hours", s.gpu_hours.toFixed(1)),
    card("Spend", "$" + s.cost_usd.toFixed(2)),
  ].join("");

  const rows = s.members || [];
  $("team-body").innerHTML = rows.length
    ? `<div class="scroll"><table><thead><tr>` +
      ["Member", "Jobs", "Succeeded", "Failed", "Running", "GPU hours", "Spend"]
        .map((h) => `<th>${h}</th>`).join("") +
      `</tr></thead><tbody>` +
      rows.map((m) => `<tr>` +
        `<td>${esc(m.user)}</td>` +
        `<td class="num">${m.jobs}</td>` +
        `<td class="num" style="color:var(--ok)">${m.succeeded}</td>` +
        `<td class="num"${m.failed ? ' style="color:var(--bad)"' : ""}>${m.failed}</td>` +
        `<td class="num">${m.running}</td>` +
        `<td class="num">${m.gpu_hours.toFixed(1)}</td>` +
        `<td class="num">$${m.cost_usd.toFixed(2)}</td>` +
        `</tr>`).join("") +
      `</tbody></table></div>`
    : empty("This team has no jobs yet.", "New job", "submit");

  // Dropping unpriced jobs without saying so would make the total a lie.
  if (s.unpriced_jobs) {
    $("team-body").innerHTML +=
      note("info",
           `${s.unpriced_jobs} ${s.unpriced_jobs === 1 ? "job is" : "jobs are"} ` +
           `not included in the spend above because no price could be worked out.`,
           s.note);
  }
}

/* ------------------------------------------------------------------ wiring */

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-goto]");
  if (b) go(b.dataset.goto);
});

document.querySelectorAll("nav button[data-view]").forEach((b) => {
  b.onclick = () => go(b.dataset.view);
});

document.querySelectorAll("#jobs-tabs button").forEach((b) => {
  b.onclick = () => {
    jobsTab = b.dataset.phase;
    poll.stop();
    // Say something immediately. The round trip is not instant, and leaving the
    // previous tab's rows on screen makes a click look like it did nothing —
    // worse, the rows shown belong to the tab the user just left.
    $("jobs-body").innerHTML = `<div class="empty"><p class="dim">Loading...</p></div>`;
    $("jobs-count").textContent = "";
    drawJobs();
  };
});

$("d-again").onclick = () => {
  if (!lastSpec) return;
  const sp = lastSpec.spec || {};
  $("f-name").value = (lastSpec.name || "") + " (rerun)";
  $("f-image").value = sp.image || "";
  $("f-command").value = Array.isArray(sp.command) ? sp.command.join(" ") : (sp.command || "");
  $("f-parallelism").value = sp.parallelism || 1;
  $("f-capacity").value = (sp.placement && sp.placement.capacityType) || "spot";
  $("f-gpu").value = (sp.resources && sp.resources.gpus && sp.resources.gpus.name) || "";
  // An entry whose value came from a Secret has no value here to copy, by
  // design. Carry the name across with an empty value and let the user fill it.
  $("f-env").value = (sp.env || [])
    .map((x) => x.fromSecret ? `${x.name}=` : `${x.name}=${x.value ?? ""}`).join("\n");
  step(1);
  go("submit");
};

/* DDPSRUN-CANCEL. A job can sit in Pending forever with nothing to do about it,
   and until this existed the only way out was kubectl — the thing this service
   exists so that nobody needs. Deleting the PacsJob is the only stop the CRD
   offers, so the row goes away rather than staying with a "cancelled" state. */
$("d-cancel").onclick = async () => {
  const jobId = $("d-id").textContent.trim();
  if (!jobId) return;
  // A browser confirm() is the one prompt available here, and this cannot be
  // undone. Naming the job in the question matters: the id is twelve hex
  // characters and the screen is often open on the wrong one.
  if (!window.confirm(`Cancel ${$("d-name").textContent}? This cannot be undone.`)) return;

  $("d-cancel").disabled = true;
  $("d-cancel").textContent = "Cancelling...";
  try {
    await call(`/v1/jobs/${jobId}` + nsQuery(), { method: "DELETE" });
    poll.stop();
    go("jobs");
  } catch (err) {
    $("d-message").innerHTML = note("err", err.message);
  } finally {
    $("d-cancel").disabled = false;
    $("d-cancel").textContent = "Cancel job";
  }
};

$("d-log-save").onclick = () => {
  // A download link is inert inside a sandboxed viewer, so open the text in a
  // tab and let the browser's own save do the work.
  const w = window.open("", "_blank");
  if (w) { w.document.write("<pre>" + esc(logText) + "</pre>"); w.document.close(); }
};

$("d-spec-copy").onclick = () => {
  navigator.clipboard?.writeText($("d-spec").textContent || "");
  $("d-spec-copy").textContent = "Copied";
  setTimeout(() => { $("d-spec-copy").textContent = "Copy"; }, 1500);
};

/* ------------------------------------------------------------------ sign in */

/*
 * DDPSRUN-UI-LOGIN. Two ways in, and the server decides which is offered.
 *
 *   Cognito, when GET /v1/login-config says enabled. The page sends the browser
 *   to Cognito's own login page, Cognito sends it back with a code, and the page
 *   trades that code for an id_token. Nothing here ever sees a password.
 *
 *   A pasted token, otherwise. That is the whole of what existed before Cognito,
 *   and it stays because a deployment with no user pool is still a supported one
 *   (`docs/16-login.md` 16.3).
 *
 * WHY PKCE. Trading a code for a token normally needs a client secret, and this
 * page is three static files anyone can read — there is nowhere to put one. PKCE
 * replaces the secret with a random number this tab generates and never sends:
 * only its SHA-256 goes out at the start, and the original goes out at the end.
 * Whoever steals the code cannot complete step two without the original.
 */

const LOGIN_KEY = "ddpsrun.pkce";      // the random number, while the round trip is in flight
const REFRESH_KEY = "ddpsrun.refresh"; // survives a tab close, unlike the id_token's hour

let loginConfig = null;

/* Where the API is. Worked out once in `start()` and used everywhere the server
   address gets written down.

   NEVER use `location.origin` for this. The page is served from CloudFront and
   the API is a Lambda Function URL, so they are different hosts. Putting
   location.origin here is what produced "The server answered 403" immediately
   after a successful sign-in on 2026-09-02: every later request went to
   CloudFront, which handed back S3's own AccessDenied for a key it has not got. */
let apiBase = "";

/* base64url with no padding, which is what OAuth asks for everywhere. */
function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function makeVerifier() {
  const raw = crypto.getRandomValues(new Uint8Array(32));
  const verifier = b64url(raw);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: b64url(digest) };
}

/* The address of this page with nothing after it. Cognito matches redirect_uri
   against its registered list character for character, so a stray ?code= left
   over from the last sign-in would make the next one fail. */
const redirectUri = () => location.origin + location.pathname;

/* DDPSRUN-UI-LOGIN-STAGE. What the login card shows while start() works:
     "checking" — fresh load, we do not yet know how this deployment signs in;
     "signing"  — back from Cognito with a ?code=, the exchange is running;
     "ready"    — start() finished; show the way in the server offers.
   One function rather than scattered .hidden writes, because the 2026-09-07
   defect was exactly a scattered default: the token box was the page's default
   face while the answer was in flight (about 0.5s warm, ~5s on a cold Lambda),
   and to someone just back from Google it read as a failed sign-in. */
function setLoginStage(stage) {
  $("login-wait").hidden = stage === "ready";
  $("login-wait-text").textContent =
    stage === "signing" ? "Signing you in…" : "Checking sign-in…";
  if (stage !== "ready") {
    $("cognito-box").hidden = true;
    $("token-box").hidden = true;
    $("token-toggle").hidden = true;
  }
}

async function startCognitoLogin() {
  const { verifier, challenge } = await makeVerifier();
  // sessionStorage, not localStorage: this value is meaningless once the round
  // trip finishes, and it should not outlive the tab that made it.
  sessionStorage.setItem(LOGIN_KEY, verifier);
  const query = new URLSearchParams({
    client_id: loginConfig.client_id,
    response_type: "code",
    scope: loginConfig.scopes.join(" "),
    redirect_uri: redirectUri(),
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.assign(`${loginConfig.login_domain}/oauth2/authorize?${query}`);
}

/* Ask Cognito's token endpoint for tokens. Used twice: once with the code after
   a sign-in, and again with the refresh token when the hour is up. */
async function exchange(body) {
  const response = await fetch(`${loginConfig.login_domain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: loginConfig.client_id, ...body }),
  });
  const parsed = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(parsed.error_description || parsed.error || "Cognito refused the exchange");
  }
  return parsed;
}

/* Called on every load. Returns true when it consumed a ?code= and signed in. */
async function finishCognitoLogin() {
  const code = new URLSearchParams(location.search).get("code");
  if (!code) return false;

  // Guard BEFORE anything is spent. Without the login domain there is no token
  // endpoint to trade the code at — the old path built "undefined/oauth2/token",
  // failed against CloudFront, and had already stripped the code from the URL,
  // so even a reload could not finish that sign-in. Leaving the URL untouched
  // keeps the code unspent: a reload asks /v1/login-config again and, when the
  // server answers this time, this function completes normally.
  if (!loginConfig || !loginConfig.enabled || !loginConfig.login_domain) {
    $("login-err").innerHTML = note("err",
      "You signed in, but the server's login settings could not be read, so the sign-in could not be finished.",
      "Reload this page to try again.");
    return false;
  }

  const verifier = sessionStorage.getItem(LOGIN_KEY);
  sessionStorage.removeItem(LOGIN_KEY);
  // Take the code out of the address bar before spending it. It is single-use,
  // and leaving it there means a reload tries to spend it twice and shows an
  // error for a sign-in that actually worked.
  history.replaceState({}, "", redirectUri());

  if (!verifier) {
    $("login-err").innerHTML = note("err",
      "This sign-in was started in a different tab, so it could not be completed.",
      "Press the sign-in button again in this tab.");
    return false;
  }

  try {
    const tokens = await exchange({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(),
      code_verifier: verifier,
    });
    store.set(apiBase || location.origin, tokens.id_token);
    if (tokens.refresh_token) localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    return true;
  } catch (err) {
    $("login-err").innerHTML = note("err", err.message);
    return false;
  }
}

/* An id_token lives an hour. Without this the screen works, then stops with a
   401 nobody asked for. `call` runs this before every request. */
async function refreshIfExpired() {
  const token = store.token;
  if (!token || !loginConfig || !loginConfig.enabled) return;

  let expiry;
  try {
    // The payload is base64url JSON. Reading `exp` here is not a security check
    // — the server verifies the signature — it only tells us when to refresh.
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    expiry = payload.exp;
  } catch {
    return;   // a pasted static token has no exp and needs no refresh.
  }
  // 60 seconds of margin, so a request does not expire in flight.
  if (!expiry || Date.now() / 1000 < expiry - 60) return;

  const refresh = localStorage.getItem(REFRESH_KEY);
  if (!refresh) { signOut(); return; }
  try {
    const tokens = await exchange({ grant_type: "refresh_token", refresh_token: refresh });
    store.set(store.server, tokens.id_token);
  } catch {
    // The refresh token is gone or revoked. Nothing to do but sign in again.
    signOut();
  }
}

function signOut() {
  poll.stop();
  store.clear();
  localStorage.removeItem(REFRESH_KEY);
  // The namespace picker belongs to the person, not the browser: the next
  // sign-in asks /v1/namespaces again from scratch.
  nsView = { loaded: false, list: [], own: "", selectable: false, current: "" };
  showApp(false);
}

function showApp(on) {
  $("login").hidden = on;
  $("bar").hidden = !on;
  document.querySelector("main").hidden = !on;
  if (on) {
    $("who-team").textContent = store.server.replace(/^https?:\/\//, "").slice(0, 32);
    route();
  }
}

$("do-login").onclick = async () => {
  $("login-err").innerHTML = "";
  const server = $("in-server").value.trim();
  const token = $("in-token").value.trim();
  if (!server || !token) {
    $("login-err").innerHTML = note("err", "Both the server address and the token are needed.");
    return;
  }
  store.set(server, token);
  try {
    await call("/v1/stats");      // one call to check the token before going in.
    showApp(true);
  } catch (err) {
    store.clear();
    $("login-err").innerHTML = note("err", err.message,
      "Check the address, and check that the token has not expired.");
  }
};

$("cognito-login").onclick = () => startCognitoLogin().catch((err) => {
  $("login-err").innerHTML = note("err", err.message);
});

$("logout").onclick = signOut;

// The token box is hidden when Cognito is on, but not removed: someone holding
// a static token for a script still has to be able to get in from a browser.
$("token-toggle").onclick = () => {
  const box = $("token-box");
  box.hidden = !box.hidden;
  $("token-toggle").textContent = box.hidden ? "Use a token instead" : "Hide";
};

window.addEventListener("hashchange", route);

/* Startup, in this order:
     1. ask the server whether Cognito is on, so the right box is drawn;
     2. if we came back from Cognito, finish that before anything else;
     3. show the app when we now hold a credential. */
(async function start() {
  // Back from Cognito? Say so before the first network round trip: the person
  // has just signed in with Google, and the worst thing to show them while the
  // exchange runs is a token box that reads as a failed sign-in.
  if (new URLSearchParams(location.search).has("code")) setLoginStage("signing");

  // Where the API lives. The page and the API are on DIFFERENT hosts in the
  // deployed setup — the page is a CloudFront distribution over an S3 bucket,
  // the API is a Lambda Function URL — so `location.origin` is NOT the server.
  // Assuming it was is what broke sign-in on 2026-09-02: CloudFront answered
  // the login-config request with S3's own 403 AccessDenied.
  //
  // The address is not committed, because it is an environment identifier and
  // this repository is meant to be opened later. Instead the release workflow
  // writes `config.json` next to these files at upload time. Fetching it is
  // always same-origin, so it always works, whatever host is serving the page.
  //
  // Two fallbacks, in order: a server the user typed in before, then this
  // page's own origin, which is correct for a same-origin deployment (the
  // server running as a pod behind one address).
  apiBase = store.server;

  const fetchLoginConfig = async (base) => {
    try {
      const response = await fetch(base + "/v1/login-config");
      return response.ok ? await response.json() : { enabled: false };
    } catch {
      return { enabled: false };
    }
  };

  // login-config needs apiBase, and config.json may override apiBase — a real
  // dependency, so a first-ever visit stays sequential. But a browser that has
  // been here before already remembers the address, and for it the two fetches
  // run in parallel (measured 2026-09-07: 317ms + 192ms sequentially, so this
  // takes ~200ms off every warm load). If config.json then names a different
  // address than remembered — it never has — the fetch simply reruns below.
  const guessedBase = apiBase;
  const early = guessedBase ? fetchLoginConfig(guessedBase) : null;
  try {
    const response = await fetch("config.json", { cache: "no-store" });
    if (response.ok) {
      const deployed = await response.json();
      if (deployed.api_base) apiBase = deployed.api_base.replace(/\/+$/, "");
    }
  } catch { /* no config.json: a pod deployment, or a local file. */ }
  if (!apiBase) apiBase = location.origin;

  loginConfig = early && apiBase === guessedBase
    ? await early
    : await fetchLoginConfig(apiBase);

  // A config that is on but incomplete is worse than one that is off: the
  // button would be live with nothing behind it, which is exactly the failure
  // this check exists to prevent.
  const cognitoOn = Boolean(
    loginConfig.enabled && loginConfig.client_id && loginConfig.login_domain
  );
  if (loginConfig.enabled && !cognitoOn) {
    $("login-err").innerHTML = note("err",
      "The server offers browser sign-in but did not say where its login page is.",
      "Ask an operator to check DDPSRUN_COGNITO_LOGIN_DOMAIN on the server.");
  }
  loginConfig.scopes = loginConfig.scopes || ["openid", "email"];

  // Remember the address so `call` has it, and so the token box does not have
  // to ask for something the page already knows.
  if (apiBase) store.set(apiBase, store.token || "");

  // Finish a Cognito return BEFORE the card offers any way in: the exchange is
  // still part of "signing you in", and revealing buttons underneath it would
  // invite a second click in the middle of the first sign-in.
  const arrived = await finishCognitoLogin();

  setLoginStage("ready");
  $("cognito-box").hidden = !cognitoOn;
  $("token-box").hidden = cognitoOn;
  $("token-toggle").hidden = !cognitoOn;
  // With the address known, the field is one less thing to get wrong.
  $("server-row").hidden = Boolean(apiBase);
  $("in-server").value = apiBase;

  showApp(arrived || Boolean(store.server && store.token));
})();
