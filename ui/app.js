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

/* The screens, DERIVED FROM THE MARKUP rather than listed by hand. It was a
   hand-written array until 2026-09-08, and the Prices screen shipped without
   being added to it: nav lit up, route() ran its draw function, the data
   arrived (610 rows, 10,702 characters of HTML in the section) — and
   show("prices") never unhid the section, because a name absent from this
   array is a name it does not touch. The page looked completely empty. (That
   screen was removed the same day for a different reason; the lesson stands.)
   Reading the ids off the DOM
   means adding a <section id="view-x"> is enough, and the three places that
   used to have to agree (markup, this array, route()) are now two. */
const VIEWS = [...document.querySelectorAll('main section[id^="view-"]')]
  .map((s) => s.id.slice("view-".length));

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
    else if (head === "submit") { show("submit"); drawImages(); drawRegionChoices(); }
    else if (head === "scripts") { show("scripts"); drawScripts(); }
    else if (head === "team")   { show("team");   drawTeam(); }
    else if (head === "vendors") { show("vendors"); drawVendors(); }
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

    // caller_cost_usd is computed on the server (the screen decides nothing):
    // the caller's own member row and nothing else. It used to fold in the
    // ownerless bucket for an operator, which made "My spend" read the whole
    // team total — see StatsResponse.caller_cost_usd.
    $("home-cards").innerHTML = [
      card("Active", running.length, running.length ? "run" : ""),
      card("Finished today", doneToday, doneToday ? "ok" : ""),
      card("Failed today", failToday, failToday ? "bad" : ""),
      card("My spend", stats ? "$" + (stats.caller_cost_usd || 0).toFixed(2) : "-"),
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
    // No owner label means it was applied with kubectl, which only an operator
    // can do. It used to say "admin", a role standing in for a person — see the
    // bucket comment in stats.summarise.
    user: (j) => esc(j.user || "kubectl"),
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

  /* DDPSRUN-UI-STALE-PANELS. Hide the two panels drawMetrics owns before asking
     about THIS job, because drawMetrics cannot hide them itself on the path that
     matters: `/v1/jobs/<id>/metrics` answers 404 for a job with no container,
     and its `catch { return; }` leaves the screen exactly as the PREVIOUS job
     left it.

     WHAT THAT LOOKED LIKE, found 2026-09-09. `c-iter2-base` has never started a
     container -- its placement cannot be filled, `kubectl get pods` in its
     namespace is empty -- and its own facts row correctly read
     "GPU: not yet known". The GPU panel under it read "360 samples, 09-04 16:39
     ~ 09-04 22:39", which is a six-hour window belonging to a job that ran four
     days earlier and was the last one looked at. A Progress bar from that job
     sat there too.

     IT IS NOT A LEAK, and that is worth saying because the shape resembles one:
     the numbers came from the previous render in this browser, so they had
     already passed DDPSRUN-OWNER-GATE for whoever is looking. The defect is
     that they were labelled as a different job's.

     WHY HERE AND NOT IN THE `catch`. This runs once per open; the catch runs on
     every 5-second poll, and hiding there would blank a running job's chart on
     one transient 502. Resetting on open is what fixes navigating between jobs,
     which is the only way the stale render was reachable. */
  $("d-gpu-panel").hidden = true;
  $("d-progress-panel").hidden = true;

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
    // DDPSRUN-COMPARE-PANEL. A Compared job's message IS the comparison, and drawCompare puts
    // it in its own panel with the four numbers pulled out. Printing it here as well showed the
    // same run-on sentence twice on one screen, a few hundred pixels apart -- measured by
    // looking at it on 2026-09-08.
    $("d-message").innerHTML = (job.message && job.phase !== "Compared")
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
      fact("Submitted by", job.user || "kubectl"),
      fact("Result", job.result_path || "-"),
    ].join("");

    drawCompare(job);
    drawShell(jobId, job);
    await Promise.all([drawMetrics(jobId, job), drawLog(jobId)]);
    // A finished job has nothing left to ask about. Stopping the timers here is
    // also where the billing for this screen stops.
    if (done) poll.stop();
  });
}

const fact = (k, v) =>
  `<div class="fact"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`;

/* The Shell panel hands the user `hyperun shell` — install line included — and
   never kubectl: a researcher with kubectl would not need this product
   (docs/00-overview.md, the founding rule). The command talks to THIS server's
   POST /v1/jobs/{id}/exec, which relays through the job's driver pod into the
   workload container on the rented machine (verified live 2026-09-07, exit
   codes relay like ssh). A terminal cannot run in this PAGE — the API is a
   Lambda Function URL, which cannot accept the inbound WebSocket a browser
   terminal needs — so the page teaches the CLI instead of pretending. */
function drawShell(jobId, job) {
  if (TERMINAL.includes(job.phase)) {
    $("d-shell").innerHTML =
      `<p class="dim">This job has finished — its containers are gone, so there is nothing to shell into.</p>`;
    return;
  }
  const key = job.job_id || jobId;
  $("d-shell").innerHTML =
    `<p class="dim small">From any terminal — no kubectl, no cloud account. One command ` +
    `per line (each is one HTTPS round trip, up to ~25s); AWS, GCP and Shadeform ` +
    `machine rentals only, because a RunPod job is a rented container with no ` +
    `machine behind it and so has no cluster to attach to:</p>` +
    `<pre class="spec">pip install hyperun\n` +
    `hyperun login --server ${esc(store.server)}\n` +
    `hyperun shell ${esc(key)}                # a prompt: type commands, 'exit' leaves\n` +
    `hyperun shell ${esc(key)} -- nvidia-smi  # run one command and exit</pre>` +
    // ★ --slot GOES BEFORE THE JOB ID, and the old wording did not say so.
    // argparse.REMAINDER consumes everything after the job id, so a flag
    // written there became part of the workload's command line and the pod
    // silently stayed 0 (fixed 2026-09-10; the CLI now refuses it instead).
    `<p class="dim tiny">parallelism &gt; 1: put --slot N BEFORE the job id ` +
    `(everything after it is sent to the workload). Not a TTY — no vim, no top.</p>`;
}

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
    const peak = m.peak_gpu || last;
    const done = job && TERMINAL.includes(job.phase);
    const from = series[0].time, to = series[series.length - 1].time;
    // m.sample_count, NOT series.length: the server thins the series to at most
    // 400 points so the chart can draw it, and job-66b46719b854 printed 785
    // readings per card and had this line say 393. An older server sends no
    // count, and then the thinned length is the only number there is.
    const taken = m.sample_count || series.length;
    $("d-gpu-note").textContent = `${taken} samples` +
      (from && to ? `, ${when(from)} ~ ${when(to)}` : `, last ${m.window_seconds}s`);

    // A FINISHED run's last reading is the idle card just before teardown —
    // 0%, 0 MiB — which read as "broken" on the screen (2026-09-07). What a
    // post-mortem actually asks for is the PEAK (memory kills runs), so a
    // terminal job leads with peak and names the final reading for what it is.
    const headline = done
      ? fact("Peak memory", `${peak.memory_used_mib} / ${peak.memory_total_mib} MiB (${peak.memory_percent.toFixed(0)}%)`) +
        // "Peak utilisation" IS NOT the utilisation inside `peak`, and printing
        // that one was a defect. `peak` is the highest-MEMORY sample, and on
        // job-66b46719b854 that single sample fell between steps and read 0%,
        // under a label ("Utilisation at peak") a reader reasonably takes to
        // mean the highest utilisation. The card's real numbers were 100% peak
        // and 84.6% mean. Both are now shown and neither is derived from the
        // memory peak. Reported 2026-09-10 by a user who knew the run had been
        // busy and saw 0%.
        fact("Peak utilisation", (m.peak_utilization_percent == null ? "-" : m.peak_utilization_percent + "%")) +
        fact("Average utilisation", (m.avg_utilization_percent == null ? "-" : m.avg_utilization_percent + "%")) +
        fact("Last reading (run ended)", `${last.utilization_percent}%, ${last.memory_used_mib} MiB`)
      : fact("Utilisation", last.utilization_percent + "%") +
        fact("Memory", `${last.memory_used_mib} / ${last.memory_total_mib} MiB (${last.memory_percent.toFixed(0)}%)`) +
        fact("Temperature", last.temperature_c + " °C") +
        fact("Power", last.power_w.toFixed(0) + " W");

    const total = peak.memory_total_mib || last.memory_total_mib || 1;

    // ONE LINE PER CARD. A job can rent several cards in one pod — baseline-c
    // rents four A100s — and until 2026-09-08 the whole panel described card 0
    // alone. `cards` is the server's per-card answer; an older server sends
    // none, and then the single series IS card 0 and the chart looks as it did.
    const cards = (m.cards && m.cards.length ? m.cards : [{ gpu_index: 0, series }]);
    const lines = cards.map((c, i) => ({
      label: `GPU ${c.gpu_index}`,
      series: c.series || [],
      color: CARD_COLORS[i % CARD_COLORS.length],
    }));

    $("d-gpu").innerHTML =
      `<div class="facts">` + headline + `</div>` +
      (cards.length > 1 ? cardTable(cards) : "") +
      chartBlock("Utilisation (%)", lines, (s) => s.utilization_percent,
                 100, ["0", "50", "100"]) +
      chartBlock("Memory (MiB)", lines, (s) => s.memory_used_mib,
                 total, ["0", String(Math.round(total / 2)), String(total)]);
  }
}

/* One colour per card. Four is what a p4d.24xlarge-shaped job needs and the
   list wraps beyond that; they are the palette's own accents rather than new
   values, so the chart stays readable in both themes. */
const CARD_COLORS = ["var(--accent)", "var(--run)", "var(--ok)", "var(--bad)"];

/* Per-card summary, drawn only when there is more than one card: with four
   cards the four "Peak memory" numbers are the first thing a post-mortem
   compares, and a chart cannot be read to the megabyte. */
function cardTable(cards) {
  return `<div class="scroll" style="margin-top:12px"><table><thead><tr>` +
    ["GPU", "Peak memory", "Peak utilisation", "Average utilisation", "Samples"]
      .map((h) => `<th>${h}</th>`).join("") +
    `</tr></thead><tbody>` +
    cards.map((c, i) => {
      const p = c.peak || c.latest || {};
      const swatch = `<i style="display:inline-block;width:9px;height:9px;border-radius:2px;` +
        `background:${CARD_COLORS[i % CARD_COLORS.length]};margin-right:6px"></i>`;
      return `<tr><td>${swatch}GPU ${c.gpu_index}</td>` +
        `<td class="num">${p.memory_used_mib == null ? "-" :
          `${p.memory_used_mib} / ${p.memory_total_mib} MiB (${(p.memory_percent || 0).toFixed(0)}%)`}</td>` +
        // ★ c.peak_utilization_percent, NOT c.peak.utilization_percent, and the
        // difference is the whole reason this column was wrong. `peak` is the
        // sample with the most MEMORY in it, and its utilisation is whatever the
        // card happened to be doing at that instant. On job-66b46719b854 all four
        // A100s reached 77,631 MiB, and the utilisation inside those four samples
        // read 0, 3, 1 and 95 -- under a heading ("Utilisation at peak") a reader
        // takes to mean the highest utilisation. Every one of those cards actually
        // peaked at 99 or 100 and averaged between 37.8 and 84.6. Worse, the
        // headline above this table already printed card 0's real peak of 100%,
        // so the same card read 100 in one panel and 0 in the next, which is what
        // the reporter saw as the cards being mixed up (2026-09-11).
        //
        // The single-card headline was fixed on 2026-09-10 and this table, eight
        // lines below it, was not.
        `<td class="num">${c.peak_utilization_percent == null ? "-" : c.peak_utilization_percent + "%"}</td>` +
        `<td class="num">${c.avg_utilization_percent == null ? "-" : c.avg_utilization_percent + "%"}</td>` +
        // Same correction as the panel note above: the readings this card
        // printed, not the points left after downsampling for the chart.
        `<td class="num">${c.sample_count || (c.series || []).length}</td></tr>`;
    }).join("") +
    `</tbody></table></div>`;
}

/* One labelled chart per quantity, ONE LINE PER CARD, drawn as SVG. No charting library: the page
   is served as static files under a CSP that blocks external hosts, and one
   more file to ship is a poor trade for a plot this small.

   This replaced a single sparkline that drew utilisation AND memory on one
   unlabelled 0-100 scale — a chart with no units, no axis values and two
   indistinguishable meanings answered nothing (user report, 2026-09-07). Each
   chart now names its unit in the title, labels three y ticks in that unit,
   and prints the wall-clock time of its first and last sample underneath. */
function chartBlock(title, lines, pick, yMax, yLabels) {
  const W = 600, H = 120, pad = 4, left = 44;   // left: room for y tick labels
  const yAt = (f) => H - pad - f * (H - pad * 2);
  // Every line is drawn on the SAME x scale — the longest series' length — so
  // four cards sampled together lie on top of each other instead of one being
  // stretched across the panel.
  const span = Math.max(1, ...lines.map((l) => l.series.length)) - 1;
  const polyline = ({ series, color }) => {
    if (!series.length) return "";
    const points = series.map((s, i) => {
      const x = left + (span ? i / span : 0) * (W - left - pad);
      const v = Math.max(0, Math.min(yMax, pick(s)));
      return `${x.toFixed(1)},${yAt(v / yMax).toFixed(1)}`;
    }).join(" ");
    return `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.6" ` +
           `stroke-linejoin="round" stroke-linecap="round"/>`;
  };
  const grid = [0, 0.5, 1].map((f, i) =>
    `<line x1="${left}" y1="${yAt(f).toFixed(1)}" x2="${W}" y2="${yAt(f).toFixed(1)}" ` +
    `stroke="var(--line)" stroke-width="1"/>` +
    `<text x="${left - 6}" y="${(yAt(f) + 4).toFixed(1)}" text-anchor="end" ` +
    `font-size="11" fill="var(--ink-dim)">${esc(yLabels[i])}</text>`).join("");
  const legend = lines.length > 1
    ? `<div class="legend">` + lines.map((l) =>
        `<span><i style="background:${l.color}"></i>${esc(l.label)}</span>`).join("") + `</div>`
    : "";
  // The x axis is read off the LONGEST series: every card is sampled by the
  // same watcher loop, so their stamps are the same to within one interval.
  const longest = lines.reduce((a, b) => (b.series.length > a.series.length ? b : a), lines[0]);
  return `<div style="margin-top:14px">` +
    `<p class="dim small" style="margin:0 0 4px">${esc(title)}</p>` +
    // Default preserveAspectRatio (uniform scale), NOT "none": the tick labels
    // are text, and a non-uniform stretch would distort every glyph.
    `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" ` +
    `aria-label="${esc(title)} over time">` + grid +
    lines.map(polyline).join("") +
    `</svg>` +
    xAxis(longest ? longest.series : []) +
    legend +
    `</div>`;
}

/* The x axis: five clock times under the plot, read off the samples themselves.
   It used to be the first and last stamp only, which is a caption rather than
   an axis — a reader could see WHEN the run started and ended and could not put
   a bump anywhere in between (user report, 2026-09-08). Five is what fits at
   this width without the labels touching. The stamps come from the apiserver's
   own timestamps on the log lines (GpuSample.time), so a gap in the readings
   shows up as an uneven spacing of the labels, which is honest: the points are
   evenly spaced on screen because they are evenly spaced in the SERIES, not in
   time. */
function xAxis(series) {
  const stamped = series.filter((s) => s.time);
  if (stamped.length < 2) return "";
  const at = (f) => stamped[Math.round(f * (stamped.length - 1))].time;
  const labels = [0, 0.25, 0.5, 0.75, 1].map(at);
  return `<div class="row" style="justify-content:space-between;padding-left:44px">` +
    labels.map((t) => `<span class="dim tiny num">${esc(when(t))}</span>`).join("") +
    `</div>`;
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

/* DDPSRUN-COMPARE-PANEL. Pull the ranking out of the one sentence the operator writes.

   THE SENTENCE IS BUILT IN internal/controller/placement.go, the `mode == placementModeCompare`
   return, and it looks like this:

     winner runpod $1.590/hr buys 1 machine; runner-up aws $2.160/hr; margin 26.4%; \
     2 of 3 candidate(s) answered (mode=compare: ...)

   It reaches status.message through truncateMsg, which CUTS AT 300 CHARACTERS -- so a long
   sentence can lose its tail, and every part below is therefore optional. A part that is missing
   is left out of the panel rather than shown as an empty row.

   WHY PARSE AT ALL, RATHER THAN PRINT THE SENTENCE. It arrives under a badge saying "Compared",
   which most readers take for a failure, in the same grey note box a real error uses. The four
   numbers a person came for -- who won, at what price, by how much, out of how many -- are what
   the panel makes findable; the sentence itself stays underneath, verbatim, because the parse can
   only ever be as good as a format nobody promised us.

   A MESSAGE THIS DOES NOT RECOGNISE IS NOT AN ERROR. `parts` comes back empty, the panel shows
   the raw text alone, and the reader is exactly where they were before this function existed. */
function parseCompare(message) {
  const text = String(message || "");
  const grab = (re) => {
    const m = text.match(re);
    return m ? m[1].trim() : "";
  };
  return {
    winner: grab(/winner\s+([^;]+?)(?:;|$)/i),
    runnerUp: grab(/runner-up\s+([^;]+?)(?:;|$)/i),
    margin: grab(/margin\s+([^;]+?)(?:;|$)/i),
    answered: grab(/(\d+\s+of\s+\d+\s+candidate\(?s?\)?\s+answered)/i),
    raw: text,
  };
}

/* Show the comparison, and hide the panels a comparison has nothing to put in.

   THE HIDING IS HALF THE FEATURE. A Compared job never rented a machine, so Result files answers
   "no files", Logs answers "the job has not started a container", the GPU chart has no samples
   and the Shell panel offers a command that cannot reach anything. Four panels each saying
   nothing, under a badge that reads like a failure, is what made a successful comparison look
   like a broken run. */
function drawCompare(job) {
  const isCompare = job.phase === "Compared";
  $("d-compare-panel").hidden = !isCompare;
  ["d-files-panel", "d-shell-panel", "d-log-panel"].forEach((id) => {
    $(id).hidden = isCompare;
  });
  if (!isCompare) return;

  const c = parseCompare(job.message);
  const rows = [
    ["Winner", c.winner],
    ["Runner-up", c.runnerUp],
    ["Margin", c.margin],
    ["Candidates", c.answered],
  ].filter(([, v]) => v);

  $("d-compare-facts").innerHTML = rows.length
    ? rows.map(([k, v]) => fact(k, v)).join("")
    : "";
  // The sentence, always, whether it parsed or not. When nothing parsed it is the only thing
  // here, which is the honest outcome for a format this panel does not own.
  $("d-compare-raw").innerHTML = note("info",
    rows.length
      ? `Nothing was rented and the workload did not run. The operator's own sentence: ${c.raw}`
      : `Nothing was rented and the workload did not run. ${c.raw}`);
}

/* ------------------------------------------------------------ 3b. Scripts */

/* DDPSRUN-SCRIPTS. Every run.sh in one namespace, GROUPED BY THE PERSON WHO RAN IT.

   THIS SAID "the scripts this caller has submitted" until 2026-09-08, and the namespace
   never guaranteed that: it is a tenancy boundary that may hold a whole team, and in this
   deployment it holds everyone. The per-person fact is each job's own ddpsrun.io/owner
   label, which the route ignored.

   WHY IT IS WORTH A SCREEN. The Script box takes a whole run.sh, and a run.sh that survived one
   job is the thing somebody wants for the next one. Without this the only way back to it was to
   remember which job used it and read that job's Submitted spec panel.

   NOTHING IS STORED FOR THIS. The server reads the text back out of the jobs themselves, so this
   screen shows exactly what is still on the cluster and nothing that is not. */
async function drawScripts() {
  // DDPSRUN-SCRIPTS-NAMESPACE. The listing is one namespace's, so it carries the
  // operator's namespace picker exactly as the Jobs screen does -- without it an
  // operator could only ever see their own, and `nsView` is already loaded.
  const sel = $("scripts-ns");
  if (nsView.loaded) {
    sel.hidden = !(nsView.selectable && nsView.list.length > 1);
    if (!sel.options.length) {
      sel.innerHTML = nsView.list.map((n) =>
        `<option value="${esc(n)}"${n === nsView.own ? " selected" : ""}>${esc(n)}</option>`
      ).join("");
      sel.onchange = () => {
        nsView.current = sel.value === nsView.own ? "" : sel.value;
        drawScripts();
      };
    }
  } else {
    sel.hidden = true;
  }

  let answer;
  const query = nsView.current ? "?namespace=" + encodeURIComponent(nsView.current) : "";
  try { answer = await call("/v1/scripts" + query); }
  catch (err) { $("scripts-list").innerHTML = note("err", err.message); return; }

  const rows = answer.scripts || [];
  // ★ THE NAMESPACE IS NOT THE PERSON, and saying "in default (yours)" was the
  // defect: all three principals in this deployment sit in `default`, so that
  // sentence separated nobody while looking as though it had. The per-person
  // fact is `owner`, from the job's own ddpsrun.io/owner label.
  const people = answer.owners || [];
  const named = people.filter(Boolean).length;
  $("scripts-note").textContent = rows.length
    ? `${rows.length} script(s) from ${named || "no"} `
      + `${named === 1 ? "person" : "people"} in namespace ${answer.namespace}`
    : `namespace ${answer.namespace || "-"}`;
  if (!rows.length) {
    $("scripts-list").innerHTML = note("info",
      answer.note || "You have not submitted a script yet.",
      "Paste a run.sh into the Script box on the New job screen and it appears here.");
    return;
  }

  // ONE SECTION PER PERSON, which is the question the screen is asked: not
  // "what scripts exist" but "what has each person run". `data-i` still indexes
  // the FLAT list, so the buttons keep working after the grouping.
  const groups = new Map();
  rows.forEach((row, i) => {
    const key = row.owner || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ row, i });
  });
  // Named people first and alphabetically; the unattributed group last, because
  // it is a backlog rather than somebody.
  const ordered = [...groups.keys()].sort((a, b) =>
    (a === "" ? 1 : b === "" ? -1 : a.localeCompare(b)));

  const card = (row, i) => {
    const last = row.created_at ? when(row.created_at) : "";
    const times = row.used > 1 ? `, run ${row.used} times` : "";
    return `<div class="panel">
      <header>
        <h2>${esc(row.name || "(unnamed)")}</h2>
        <span class="dim small">${row.lines} line(s)${esc(times)}${last ? "  last run " + esc(last) : ""}</span>
        <div class="spacer"></div>
        <button class="go tiny use-script" data-i="${i}" style="padding:4px 10px">Use this</button>
        <button class="flat tiny get-script" data-i="${i}" style="padding:4px 10px">Download</button>
      </header>
      <pre class="spec">${esc(row.script)}</pre>
    </div>`;
  };

  $("scripts-list").innerHTML = ordered.map((who) => {
    const mine = groups.get(who);
    const heading = who
      ? `${esc(who)} <span class="dim small">- ${mine.length} script(s)</span>`
      : `<span class="dim">Submitter not recorded</span> `
        + `<span class="dim small">- ${mine.length} script(s)</span>`;
    const why = who ? "" : note("info",
      "These jobs were not created through this service, so no submitter was "
      + "recorded on them and it cannot be recovered afterwards.");
    return `<h2 style="margin:18px 0 6px">${heading}</h2>${why}`
      + mine.map(({ row, i }) => card(row, i)).join("");
  }).join("");

  $("scripts-list").querySelectorAll("button.get-script").forEach((b) => {
    b.onclick = () => {
      const row = rows[Number(b.dataset.i)];
      // The filename comes from the server, built from the job's display name --
      // a browser download needs a name and "download" is not one.
      saveText(row.script, row.filename || "run.sh");
    };
  });

  $("scripts-list").querySelectorAll("button.use-script").forEach((b) => {
    b.onclick = () => {
      // Fill the box and go, rather than submitting: the image, the GPU and the capacity type
      // are this job's decisions and the previous job's are not necessarily right for it.
      $("f-command").value = rows[Number(b.dataset.i)].script;
      $("f-command-note").innerHTML = note("info",
        "Loaded from a previous job. The image, the GPU and the capacity type are still yours to set.");
      step(1);
      go("submit");
    };
  });
}


/* DDPSRUN-SCRIPT-FILE. Load a run.sh off the machine into the Script box.

   Entirely in the browser: FileReader reads the chosen file and the text goes
   into the same textarea a paste would fill. Nothing is uploaded, because
   nothing is stored -- the script rides on the job inside `args`, which is what
   lets the Scripts screen hand it back afterwards.

   A SIZE CEILING, because the script travels in the job object. A PacsJob lives
   in etcd and Kubernetes refuses an object over roughly 1.5 MB; a script that
   big is a data file somebody picked by mistake, and finding out at submit time
   would mean a 400 from the apiserver about object size. 256 KB is far above any
   real run.sh (the largest in this lab is under 4 KB) and far below the ceiling. */
const SCRIPT_FILE_MAX = 256 * 1024;

function wireScriptFile() {
  const input = $("f-script-file");
  const noteAt = $("f-script-file-note");

  input.onchange = () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (file.size > SCRIPT_FILE_MAX) {
      noteAt.textContent =
        `${file.name} is ${humanSize(file.size)} — too big for a script.`;
      input.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => { noteAt.textContent = `Could not read ${file.name}.`; };
    reader.onload = () => {
      $("f-command").value = String(reader.result || "");
      const lines = $("f-command").value.split("\n").length;
      noteAt.textContent = `${file.name} — ${lines} line(s) loaded`;
      $("f-command-note").innerHTML = note("info",
        "Loaded from a file. The text is what gets submitted, so edits here are "
        + "what runs — the file on your machine is not read again.");
    };
    reader.readAsText(file);
  };

  $("f-script-save").onclick = () => {
    const text = $("f-command").value;
    if (!text.trim()) { noteAt.textContent = "The Script box is empty."; return; }
    const name = ($("f-name").value.trim() || "run").replace(/[^A-Za-z0-9._-]+/g, "-");
    saveText(text, `${name}.sh`);
    noteAt.textContent = `Saved as ${name}.sh`;
  };
}

/* Hand the browser a file to save. Used by the Script box and by every entry on
   the Scripts screen.

   The object URL is revoked on the next tick rather than immediately: the click
   only STARTS the save, and revoking in the same statement can cancel it in
   some browsers. */
function saveText(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}


/* ------------------------------------------------------------------ Prices */

/* DDPSRUN-PRICES. The catalogue's price table, every region of it.

   ONE FETCH, then filtered in the browser. The answer is about 610 rows and some
   70 KB, which is deliberate: a price table is used by sorting and narrowing it,
   and a round trip per card would make every narrowing feel slow for no benefit.

   NOTHING IS COMPUTED HERE. Every number is the server's, and the two price
   BASES are kept in separate tables — an AWS row is a whole machine, a GCP row is
   the accelerators alone — because ranking them together would put GCP on top
   whenever it is not actually cheaper. */
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
    parallelism: num("f-parallelism") || 1,
    capacity_type: $("f-capacity").value,
    env,
    training: {},
  };

  /* DDPSRUN-UI-COMMAND. The box holds ONE SHELL LINE and it goes out as
     args ["bash","-lc", line].

     WHAT WAS WRONG. It used to go out as `command: "<line>"` -- a bare string
     into a field the server types as `list[str]` -- so every non-empty box made
     Validate answer 422 "command: Input should be a valid list". The
     placeholder shipped in index.html was itself a failing input, which means
     the only submittable state of this screen was an empty Command box running
     whatever the image's own ENTRYPOINT is.

     WHY args AND NOT command. `command` overrides the entrypoint and takes an
     argv array, so honouring a typed line would mean splitting it -- and a
     splitter gets `python -c "import x"` wrong four ways. Handing the line to a
     shell is what makes quotes, pipes and redirections mean what they look
     like, and it is the shape /v1/explain's own example uses:
     "args": ["bash", "-lc", "python train.py --epochs 4"]. */
  const line = $("f-command").value.trim();
  if (line) {
    body.args = ["bash", "-lc", line];
    /* DDPSRUN-UI-SCRIPT. THE SAME TEXT, SENT TWICE, ON PURPOSE.

       `args` is what RUNS. `script` is what validate READS, and four of its checks read nothing
       else: the adapter-path pair, the exit trap that saves partial results, the two length caps
       that have to agree, and the TRL patch. Sending only `args` meant those four could never
       run from this screen, whatever anybody pasted -- and validate said so in its `not_checked`
       list, which this screen also did not draw. So the box looked checked and was not.

       The server throws `script` away after reading it ("read and thrown away, never stored"),
       so sending it costs one field on one request and stores nothing extra. */
    body.script = line;
  }

  if ($("f-gpu").value) {
    body.gpu = { name: $("f-gpu").value, count: num("f-gpucount") || 1 };
  }

  /* DDPSRUN-VENDOR-CHOICE. Nothing checked sends nothing, which is "no
     restriction" and is what every job did before these boxes existed. */
  const vendors = Array.from(document.querySelectorAll("#view-submit [data-vendor]"))
    .filter((b) => b.checked).map((b) => b.dataset.vendor);
  if (vendors.length) body.vendors = vendors;
  if ($("f-mode").value) body.placement_mode = $("f-mode").value;
  // DDPSRUN-REGIONS. Comma or whitespace separated, sent verbatim: PACSrun's own
  // spelling is either a bare vendor ("gcp") or a vendor and region
  // ("aws/us-east-1"), and rewriting what somebody typed would hide a typo that
  // the server can name precisely.
  const regions = ($("f-regions").value || "")
    .split(/[\s,]+/).map((r) => r.trim()).filter(Boolean);
  if (regions.length) body.regions = regions;

  const t = body.training;
  if (num("f-pairs")) t.pairs = num("f-pairs");
  if (num("f-epochs")) t.epochs = num("f-epochs");
  // DDPSRUN-TRAINING-FACTS. row_tokens is the one that turns the estimate from "unknown" into a
  // number: pairs and epochs give the STEP COUNT, and this gives the seconds each step takes.
  // The screen collected the other three and not this one, so it could answer how many steps and
  // never how long.
  if (num("f-rowtokens")) t.row_tokens = num("f-rowtokens");
  if (num("f-cap")) t.cap = num("f-cap");
  if (num("f-batch")) t.batch_size = num("f-batch");

  return body;
}

/* DDPSRUN-IMAGES. Fill the Image box's datalist with what this lab has already built.

   WHY IT IS FETCHED WHEN THE SCREEN OPENS AND CACHED. The list changes when somebody pushes an
   image, which is not while a form is being filled in; asking once per visit costs one Lambda
   call against a form that takes minutes to complete.

   A FAILURE HERE CHANGES NOTHING ABOUT THE FORM. The box is a free-text input with a datalist,
   not a select, so an empty list leaves it exactly as usable as it was before this existed -- and
   a public image (runpod/pytorch:...) was never in the list anyway, because it is not in our
   registry. So the catch says what happened in the note beside the label and moves on.

   THE NOTE IS THE PART THAT MATTERS ON A FAILURE. The server answers 200 with a `note` rather
   than 502 for exactly this reason: an empty list with no explanation reads as "this lab has
   built nothing", which would send an operator looking in the wrong place when what is actually
   missing is the IAM policy (DDPSRUN-IMAGES-READ in terraform/lambda). */
let imagesDrawn = false;

/* The one row of /v1/prices this screen still keeps: the REGION NAMES. The
   Prices screen that used to draw the whole table is gone (2026-09-08) —
   two vendors out of the many that exist is not a price comparison, and
   GPU Compass reads the same SkyPilot catalogue we do — but the names are
   still what `placement.regions` accepts, and a datalist of real ones beats
   a free-text box that silently takes a typo. */
let priceRows = null;

/* DDPSRUN-REGIONS. Free text stays free text: `placement.regions` also takes a
   bare vendor word, which is not in this list. Failure is silent on purpose --
   the box works without the suggestions, and a person filling in a form does
   not need a network error about a dropdown. */
async function drawRegionChoices() {
  const list = $("f-region-list");
  if (list.options.length) return;
  try {
    const answer = priceRows || await call("/v1/prices");
    priceRows = answer;
    list.innerHTML = (answer.regions || [])
      .map((r) => `<option value="aws/${esc(r)}">`).join("");
    $("f-regions-note").innerHTML = note("info",
      `Blank means ${esc(answer.default_region)} and nothing else \u2014 an AWS ask `
      + `that names no region gets the operator's one default, not a search. `
      + `${(answer.regions || []).length} AWS regions are on offer. `
      + `Comparing prices across vendors: <a href="https://gpus.skypilot.co/" `
      + `target="_blank" rel="noopener">GPU Compass</a>, which reads the same `
      + `SkyPilot catalogue this service does.`);
  } catch { /* the box is free text and works without suggestions. */ }
}

async function drawImages(force) {
  if (imagesDrawn && !force) return;
  imagesDrawn = true;
  let answer;
  try {
    answer = await call("/v1/images");
  } catch (err) {
    $("f-image-note").textContent = `image list unavailable (${err.message})`;
    return;
  }
  const rows = answer.images || [];
  const options = [];
  rows.forEach((r) => (r.addresses || []).forEach((a) => options.push([a, r.pushed_at])));
  // A datalist option's `label` is what the browser shows beside the value, so the push date
  // rides along without becoming part of what gets typed into the box.
  $("f-image-list").innerHTML = options
    .map(([value, pushed]) => `<option value="${esc(value)}"${pushed ? ` label="${esc(String(pushed).slice(0, 10))}"` : ""}></option>`)
    .join("");
  const repos = rows.filter((r) => (r.addresses || []).length).length;
  $("f-image-note").textContent = answer.note
    ? answer.note
    : options.length
      ? `${options.length} from ${repos} ${repos === 1 ? "repository" : "repositories"} this lab has built` +
        (answer.truncated ? ", and more than one page exists" : "")
      : "";

  /* The visible half. One block per repository, newest push first, its tags as buttons -- so
     the list can be READ without knowing a datalist is there, and a screenshot shows it. */
  $("f-image-picker").innerHTML = rows.length
    ? rows.map((r) => {
        const addrs = r.addresses || [];
        const when = r.pushed_at ? String(r.pushed_at).slice(0, 10) : "";
        const tags = addrs.length
          ? addrs.map((a, i) => `<button class="flat tiny pick" type="button" data-image="${esc(a)}"`
              + ` style="padding:3px 8px">${esc(r.tags[i] || a)}</button>`).join(" ")
          : `<span class="dim tiny">no tagged image</span>`;
        return `<div style="margin:6px 0">`
          + `<div class="dim tiny mono">${esc(r.repository)}${when ? "  " + esc(when) : ""}</div>`
          + `<div class="row">${tags}</div></div>`;
      }).join("")
    : note("info", answer.note || "This account holds no container repositories.");

  $("f-image-picker").querySelectorAll("button.pick").forEach((b) => {
    b.onclick = () => {
      $("f-image").value = b.dataset.image;
      $("f-image-picker").hidden = true;
      $("f-image-toggle").textContent = "Browse this lab's images";
    };
  });
}

$("f-image-toggle").onclick = () => {
  const box = $("f-image-picker");
  box.hidden = !box.hidden;
  $("f-image-toggle").textContent = box.hidden
    ? "Browse this lab's images" : "Hide the list";
  /* Uses what the view-entry fetch already got, and only asks again when that produced nothing.
     MEASURED 2026-09-08: /v1/images takes about 4 s against the real registry, because it asks
     the registry once for the repository list and then once per repository for its tags -- 19
     repositories here, so 20 round trips. Refetching on every toggle would spend that again for
     a list that changes when somebody pushes an image, which is not while a form is open. */
  if (!box.hidden && !$("f-image-picker").innerHTML) drawImages(true);
};

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

  /* DDPSRUN-UI-NOT-CHECKED. What no check could look at, printed under the findings.

     WHY IT HAS TO BE ON SCREEN. The server has always answered this list and this screen threw
     it away, so "Nothing to flag." read as "everything is fine" -- and the server's own words
     for the field are that it is "Listed rather than passed over in silence, so a clean result
     is not mistaken for a complete one". A clean validate on a job whose script was never sent
     was the worst version of that: four checks had not run and nothing said so.

     It is collapsed by default. The list is five items long on every request and it is context,
     not a verdict; open on every visit it would push the findings themselves off the screen. */
  const notChecked = v.not_checked || [];
  $("s2-not-checked").innerHTML = notChecked.length
    ? `<details><summary class="dim small">${notChecked.length} thing(s) no check could look at</summary>`
      + `<ul class="dim small">${notChecked.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>`
    : "";

  // With an error present the next button does not work (15.10), and it says
  // what has to happen instead of just going grey.
  $("s2-next").disabled = errors.length > 0;
  $("s2-next").textContent = errors.length
    ? `Fix ${errors.length} ${errors.length === 1 ? "error" : "errors"} first`
    : "See the cost";
  step(2);
};

$("s1-reset").onclick = () => {
  // f-result is gone (DDPSRUN-UI-NO-RESULT-PATH) and clearing an id that no
  // longer exists throws on $(id).value, which would have left every field after
  // it uncleared.
  ["f-name", "f-image", "f-command", "f-env",
   "f-pairs", "f-epochs", "f-rowtokens", "f-cap", "f-batch"].forEach((id) => { $(id).value = ""; });
  $("f-command-note").innerHTML = "";
  $("f-gpucount").value = 1;
  $("f-parallelism").value = 1;
  $("f-gpu").value = "";
  $("f-capacity").value = "spot";
  $("f-mode").value = "";
  document.querySelectorAll("#view-submit [data-vendor]").forEach((b) => { b.checked = false; });
  vendorRules();
  $("s1-err").innerHTML = "";
};

/* DDPSRUN-VENDOR-CHOICE. gcp, azure, lambda and nebius are answered from
   catalogue CSVs and no actuator in PACSrun understands their machine names, so
   checking one only makes sense under `compare`, which ranks the candidates and
   then stops without buying anything. Under `ordered` or `cheapest` such a
   vendor can win the walk and the job then fails at the actuator with the
   comparison thrown away.

   THE SCREEN FORCES IT AND THE SERVER ONLY WARNS, and the two disagreeing is
   deliberate. PACSrun's CRD allows the combination on purpose ("the right answer
   to 'buy me a thing nobody can buy'"), so /v1/validate answers a WARNING and
   lets it through -- a caller driving the API can still do it. What the screen
   must not do is offer a checkbox whose only outcome is a failed job. */
function vendorRules() {
  const priced = Array.from(document.querySelectorAll("#view-submit [data-priced]"))
    .filter((b) => b.checked).map((b) => b.dataset.vendor);
  const mode = $("f-mode");
  if (priced.length) {
    mode.value = "compare";
    Array.from(mode.options).forEach((o) => { o.disabled = o.value !== "compare"; });
    $("f-mode-note").innerHTML = note("info",
      `${priced.join(", ")} can be priced but not rented, so this is a comparison only: ` +
      `nothing is bought and the job ends in the phase Compared with the winner and the ` +
      `margin in its message.`);
  } else {
    Array.from(mode.options).forEach((o) => { o.disabled = false; });
    $("f-mode-note").innerHTML = mode.value === "compare"
      ? note("info", "compare prices every candidate and then STOPS. No machine is rented and " +
                     "the workload does not run.")
      : "";
  }
}

document.querySelectorAll("#view-submit [data-vendor]")
  .forEach((b) => { b.onchange = vendorRules; });
$("f-mode").onchange = vendorRules;

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
  // DDPSRUN-AWS-PRICES. The hourly rate is drawn even when the hours are not,
  // and that is the whole reason this card exists. Twelve of the fourteen cards
  // in the GPU dropdown have no throughput measurement, so "Estimated cost"
  // read "unknown" for all twelve -- and the screen said nothing else about
  // money, though the machine each one needs has a published price. Rate comes
  // from the server (EstimateResponse.rate); nothing is computed here.
  const rate = e.rate || {};
  const rateText = (rate.usd_per_hour_low == null) ? "unknown"
    : rate.usd_per_hour_low === rate.usd_per_hour_high
      ? `$${rate.usd_per_hour_low.toFixed(4)}/h`
      : `$${rate.usd_per_hour_low.toFixed(4)} - $${rate.usd_per_hour_high.toFixed(4)}/h`;

  $("s3-basis").textContent = e.basis;
  $("s3-cards").innerHTML = [
    card("Hourly rate", rate.vendor
      ? `${rateText} (${rate.vendor}${rate.machines > 1 ? ", " + rate.machines + " machines" : ""})`
      : rateText),
    card("Estimated cost", money(e.cost_usd)),
    card("Estimated time", hours(e.hours)),
    card("Steps", e.steps ?? "unknown"),
    card("Capacity type", e.capacity_type),
  ].join("");

  const notes = [];
  // With confidence "unknown" there is no time and no total. Which of the two
  // halves is missing decides what the note can honestly advise: with a rate in
  // hand the user can still bound the spend by capping the run.
  if (e.hours.confidence === "unknown") {
    notes.push(rate.usd_per_hour_low == null
      ? note("warn", "No measured run and no price for this ask.",
             "Neither the time nor the cost can be answered. The findings below say why.")
      : note("warn", `No measured run on this card, so the total is unknown. The rate is not: ${rateText}.`,
             "One hour of this job is a known number. A short trial run measures the rest, "
             + "and a second estimate then answers the total."));
  } else {
    notes.push(note("info",
      `Basis: ${e.hours.confidence === "measured" ? "a measured run" : "interpolation between measured runs"}`));
  }
  if (rate.basis) notes.push(note("info", `Price: ${rate.basis}`));
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
    // "Tracked", because this is a floor, not the bill: only jobs still in
    // the cluster count, and only from their startedAt (SCOPE_NOTE below).
    // On 2026-09-08 a plain "Spend" of $62 was read as the whole bill (~$103
    // attributed on the vendor side) and cost an hour of doubt.
    card("Tracked spend", "$" + s.cost_usd.toFixed(2)),
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
    : empty(s.jobs ? "No job here was submitted by a person."
                   : "This team has no jobs yet.", "New job", "submit");

  // A job applied with kubectl carries no ddpsrun.io/owner label, so there is
  // nobody to put in the Member column -- and the three names that column has
  // tried (`default`, `admin`, `kubectl`) were each read as a colleague. Its
  // spend is in the totals above, so saying nothing here would leave the table
  // quietly short of the team figure.
  if (s.unowned_jobs) {
    $("team-body").innerHTML +=
      note("info",
           `${s.unowned_jobs} ${s.unowned_jobs === 1 ? "job is" : "jobs are"} ` +
           `in the totals above but in no row below.`,
           "They were applied straight to the cluster with kubectl, so they "
           + "carry no owner and belong to no member.");
  }

  // Dropping unpriced jobs without saying so would make the total a lie.
  if (s.unpriced_jobs) {
    $("team-body").innerHTML +=
      note("info",
           `${s.unpriced_jobs} ${s.unpriced_jobs === 1 ? "job is" : "jobs are"} ` +
           `not included in the spend above because no price could be worked out.`,
           s.note);
  }
  $("team-body").innerHTML += SCOPE_NOTE;
}

/* What the money on this screen IS, stated where the money is shown. Written
   after 2026-09-08, when a bare "$62" was read as the whole bill: the vendor
   had charged about $103 for the same work, and every missing dollar had one
   of these four explanations. */
const SCOPE_NOTE = `<p class="dim tiny" style="margin-top:12px">` +
  `These figures are a tracked floor, not the bill: only jobs still in the ` +
  `cluster count (a job deleted, or resubmitted under the same name, takes its ` +
  `record with it), hours start at each job's own startedAt (jobs finished ` +
  `before 2026-08-29 carry no clock and count zero), unpriced machines add ` +
  `hours but no dollars, and the vendor also bills image pull and idle ` +
  `minutes outside our window. The vendor's own bill is the authority.</p>`;

/* ------------------------------------------------------------- 5. Vendors */

/* The same /v1/stats answer, read by WHO SOLD the machines. The server adds
   both tables up in one pass over the same jobs, so this screen and Team can
   never disagree about a dollar. */
async function drawVendors() {
  let s;
  try { s = await call("/v1/stats"); }
  catch (err) { $("vendors-body").innerHTML = note("err", err.message); return; }

  $("vendors-note").textContent = s.team ? `team ${s.team}` : "";
  const rows = s.vendors || [];
  // The table is the whole catalogue now, so a bare row count would read `7`
  // for every team forever and say nothing. What is worth a card is how many
  // of them this team has actually bought from.
  const used = rows.filter((v) => v.jobs > 0).length;
  $("vendors-cards").innerHTML = [
    card("Vendors used", `${used} of ${rows.length}`),
    card("GPU hours", s.gpu_hours.toFixed(1)),
    card("Tracked spend", "$" + s.cost_usd.toFixed(2)),
  ].join("");

  // A job that never reached Running rented nothing, so the server files it
  // under no vendor at all (DDPSRUN-STATS, VendorTotals). Silently showing
  // fewer jobs here than Team shows is the kind of quiet gap this screen's
  // SCOPE_NOTE exists to refuse, so the difference is stated outright.
  const placed = rows.reduce((sum, v) => sum + v.jobs, 0);
  const unplaced = s.jobs - placed;

  $("vendors-body").innerHTML = (rows.length
    ? `<div class="scroll"><table><thead><tr>` +
      ["Vendor", "Jobs", "GPU hours", "Spend", "Unpriced jobs"]
        .map((h) => `<th>${h}</th>`).join("") +
      `</tr></thead><tbody>` +
      rows.map((v) => `<tr>` +
        `<td>${esc(v.vendor)}</td>` +
        `<td class="num">${v.jobs}</td>` +
        `<td class="num">${v.gpu_hours.toFixed(1)}</td>` +
        `<td class="num">$${v.cost_usd.toFixed(2)}</td>` +
        `<td class="num"${v.unpriced_jobs ? ' style="color:var(--run)"' : ""}>${v.unpriced_jobs}</td>` +
        `</tr>`).join("") +
      `</tbody></table></div>`
    : empty("No vendor is configured on this deployment.", "New job", "submit"))
    + (unplaced > 0
        ? note("info",
               `${unplaced} of the team's ${s.jobs} ${s.jobs === 1 ? "job" : "jobs"} ` +
               `${unplaced === 1 ? "is" : "are"} not in this table.`,
               "They never reached Running, so no vendor sold them a machine. They "
               + "are still counted on the Team screen.")
        : "")
    + SCOPE_NOTE;
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

/* DDPSRUN-UI-RERUN. Copy the submitted spec back into the form.

   THREE THINGS IT USED TO DROP, all of them silently:

     args        it read `command` only. args is how a one-line workload is
                 expressed (the server's own words) and is what this screen now
                 sends, so a job submitted from here came back with NO command
                 at all -- and the rerun would then run the image's entrypoint.
     gpus.vramGB the server writes EITHER `name` OR `vramGB`, never both
                 (models.py, to_pacsjob). Reading `name` alone meant a job that
                 asked for 48 GB came back with the GPU box empty.
     placement   vendors and mode were not in the object at all until
                 2026-09-08; now they are, so they come back too.

   WHAT IT STILL CANNOT RESTORE, said out loud rather than half-done: the
   training facts (pairs, epochs, cap, batch) are not stored anywhere. They are
   arguments to the estimate, not part of the job, so the object has no copy to
   read -- and the estimate is re-run on the way to submitting anyway. */
$("d-again").onclick = () => {
  if (!lastSpec) return;
  const sp = lastSpec.spec || {};
  $("f-name").value = (lastSpec.name || "") + " (rerun)";
  $("f-image").value = sp.image || "";

  // A line this screen sent comes back as ["bash","-lc","<line>"], so unwrap
  // that shape back into the one box it came from. Anything else -- a kubectl
  // job's argv, an entrypoint override -- is joined with spaces, which is
  // readable and re-sendable because the box is handed to a shell.
  const a = Array.isArray(sp.args) ? sp.args : [];
  const shellLine = (a.length === 3 && a[0] === "bash" && a[1] === "-lc") ? a[2] : "";
  $("f-command").value = shellLine
    || a.join(" ")
    || (Array.isArray(sp.command) ? sp.command.join(" ") : (sp.command || ""));

  $("f-parallelism").value = sp.parallelism || 1;

  const pl = sp.placement || {};
  $("f-capacity").value = pl.capacityType || "spot";
  $("f-mode").value = pl.mode && pl.mode !== "ordered" ? pl.mode : "";
  const want = new Set(pl.vendors || []);
  document.querySelectorAll("#view-submit [data-vendor]")
    .forEach((b) => { b.checked = want.has(b.dataset.vendor); });
  vendorRules();

  // The GPU box lists MODELS, so a job that asked by memory has nothing to
  // select. Leaving it empty means "let the server recommend one", which is the
  // honest answer -- and the note says why rather than letting the ask vanish.
  const g = (sp.resources && sp.resources.gpus) || {};
  $("f-gpu").value = g.name || "";
  $("f-gpucount").value = g.count || 1;
  $("s1-err").innerHTML = (!g.name && g.vramGB)
    ? note("info", `The original asked for ${g.vramGB} GB rather than a model, and this box ` +
                   `lists models. Left empty, which means "let the server recommend one".`)
    : "";

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
   offers, so the row goes away rather than staying with a "cancelled" state.

   THE BUTTON SAYS "Delete job" SINCE 2026-09-10, and it used to say "Cancel
   job". The old word described an intention and set the wrong expectation: a
   person who cancels expects the job to still be listed, and on 2026-09-09 one
   asked why a cancelled job showed no cancelled state. It has none because the
   object is gone. The confirm names what survives — the result path — because
   that is the part people are actually afraid of losing. */
$("d-cancel").onclick = async () => {
  const jobId = $("d-id").textContent.trim();
  if (!jobId) return;
  // A browser confirm() is the one prompt available here, and this cannot be
  // undone. Naming the job in the question matters: the id is twelve hex
  // characters and the screen is often open on the wrong one.
  if (!window.confirm(
        `Delete ${$("d-name").textContent}?\n\n` +
        `It stops and disappears from the list — there is no cancelled state to ` +
        `look at afterwards. Files already written to its result path stay.`)) return;

  $("d-cancel").disabled = true;
  $("d-cancel").textContent = "Deleting...";
  try {
    await call(`/v1/jobs/${jobId}` + nsQuery(), { method: "DELETE" });
    poll.stop();
    go("jobs");
  } catch (err) {
    $("d-message").innerHTML = note("err", err.message);
  } finally {
    $("d-cancel").disabled = false;
    $("d-cancel").textContent = "Delete job";
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
    $("newcomer").hidden = true;
  }
}

/* DDPSRUN-REGISTER. The address inside an id_token, FOR DISPLAY ONLY.

   This decodes the token's payload without checking its signature, and that is
   safe for exactly one reason: the token is the browser's own, so the only
   person who could have forged it is the person reading the screen. Nothing is
   decided here. Every route re-verifies the token against the pool's live JWKS
   (`cognito.Verifier`), so a tampered payload changes what this label says and
   nothing else.

   Returns the address, or "" for a static token or anything unparseable — the
   newcomer screen then simply has no address to show, which is a worse screen
   and not a wrong one. */
function emailInToken(token) {
  const parts = String(token || "").split(".");
  if (parts.length !== 3) return "";
  try {
    // base64url -> base64, then decodeURIComponent so a non-ASCII address
    // survives: atob yields bytes, not characters.
    const json = decodeURIComponent(
      atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"))
        .split("").map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
        .join(""));
    return JSON.parse(json).email || "";
  } catch { return ""; }
}

/* Whether the credential we hold can actually reach anything, and which screen
   that means.

   WHY IT PROBES AT ALL. A good Cognito token and no namespace is a real state
   (403 on every route), and before this the app opened into it and filled with
   errors. GET /v1/namespaces is the probe because it is the cheapest
   authenticated route there is: it answers out of the token file and needs no
   Kubernetes permission, so an unregistered caller costs the cluster nothing.

   Returns "in", "newcomer", or "out". */
async function probeAccess() {
  if (!store.server || !store.token) return "out";
  try {
    await call("/v1/namespaces");
    return "in";
  } catch (err) {
    // `call` throws the server's `detail` string, so the status is gone by
    // here. The sentence is the server's own and both halves of it are stable
    // (`auth.principal_for_email`), which is why this matches on the text
    // rather than re-issuing the request to read a code.
    return /not registered with this service/.test(err.message)
      ? "newcomer" : "out";
  }
}

/* Draw the newcomer screen. Called only when probeAccess says so. */
function showNewcomer() {
  poll.stop();
  $("login").hidden = false;
  $("bar").hidden = true;
  document.querySelector("main").hidden = true;
  $("cognito-box").hidden = true;
  $("token-box").hidden = true;
  $("login-err").innerHTML = "";
  $("nc-email").textContent = emailInToken(store.token) || "an address we cannot read";
  const canAsk = Boolean(loginConfig.registration_requests);
  $("nc-ask").hidden = !canAsk;
  $("nc-ask-note").hidden = !canAsk;
  $("nc-no-ask").hidden = canAsk;
  $("newcomer").hidden = false;
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

/* Sign out for real: this browser forgets us, and so does Cognito.

   REMOVING EVERY ddpsrun.* KEY, not the three we happen to name. A key left
   behind starts the next visit half signed in, and the list has grown twice
   already (refresh token, then the PKCE verifier).

   AND ENDING THE COGNITO SESSION, which is the half that was missing until
   2026-09-08. Clearing local storage leaves Cognito's own session cookie
   alive, so the next "Sign in" bounced straight back with the SAME account —
   no account chooser, no way to sign in as anybody else, and it read as a
   sign-out that had not happened. `logout_uri` must match a URL registered in
   terraform/cognito's logout_urls character for character. */
function signOut() {
  poll.stop();
  $("newcomer").hidden = true;
  Object.keys(localStorage)
    .filter((k) => k.startsWith("ddpsrun."))
    .forEach((k) => localStorage.removeItem(k));
  sessionStorage.removeItem(LOGIN_KEY);
  // The namespace picker belongs to the person, not the browser: the next
  // sign-in asks /v1/namespaces again from scratch.
  nsView = { loaded: false, list: [], own: "", selectable: false, current: "" };

  if (loginConfig && loginConfig.enabled && loginConfig.login_domain && loginConfig.client_id) {
    const query = new URLSearchParams({
      client_id: loginConfig.client_id,
      logout_uri: redirectUri(),
    });
    // The page leaves here; Cognito drops its cookie and sends the browser
    // back to redirectUri(), where start() runs and draws the login card.
    location.assign(`${loginConfig.login_domain}/logout?${query}`);
    return;
  }
  showApp(false);
}

function showApp(on) {
  // The newcomer card is switched OFF here and turned on only by
  // showNewcomer(). It used to be left alone, and that is what made Sign out
  // look broken (2026-09-08): signOut cleared the tokens and called
  // showApp(false), the login card came back UNDERNEATH a newcomer card that
  // nothing had hidden, and the reader saw the same "Signed in as ..." screen
  // and concluded the button had not worked.
  $("newcomer").hidden = true;
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
$("nc-out").onclick = signOut;
wireScriptFile();

$("nc-ask").onclick = async () => {
  $("nc-ask").disabled = true;
  $("nc-result").innerHTML = "";
  try {
    const answer = await call("/v1/register-request", { method: "POST" });
    // The server distinguishes "sent now" from "an earlier press already sent
    // it", and both are successes. Saying which one prevents a second press
    // reading as a failure.
    $("nc-result").innerHTML = note("info", answer.message,
      "You can close this page. Sign in again once an operator tells you they "
      + "have added you.");
  } catch (err) {
    $("nc-result").innerHTML = note("err", err.message);
    $("nc-ask").disabled = false;
  }
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
  // Token sign-in is gone from the UI (2026-09-08, user decision: "oauth로만").
  // With Cognito on, the only way in is Google. The token box survives ONLY as
  // the sole fallback for a deployment that has no user pool at all — there it
  // is the only way in, so hiding it would lock everyone out. It never appears
  // alongside Cognito, and the "Use a token instead" toggle is gone entirely.
  $("token-box").hidden = cognitoOn;
  // With the address known, the field is one less thing to get wrong.
  $("server-row").hidden = Boolean(apiBase);
  $("in-server").value = apiBase;

  // WHY THIS PROBES INSTEAD OF JUST OPENING. Holding a credential is not the
  // same as being able to use it. A first-time Google visitor holds a perfectly
  // good token and is 403 everywhere, and this line used to open the app for
  // them: every panel then showed the same 403 sentence, which reads as a
  // broken service rather than an account nobody has registered yet.
  if (arrived || (store.server && store.token)) {
    const access = await probeAccess();
    if (access === "in") showApp(true);
    else if (access === "newcomer") showNewcomer();
    else signOut();
  } else {
    showApp(false);
  }
})();
