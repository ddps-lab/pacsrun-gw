/* submit_form_test.js -- what the New job screen actually sends.
 *
 * WHY THIS FILE EXISTS. Until 2026-09-08 nothing in CI looked at ui/ at all: not pytest, which
 * cannot read JavaScript, and not even `node --check`. Two defects shipped through that gap and
 * both were invisible from either side on its own.
 *
 *   1. The Command box sent a bare STRING into `command`, which the server types as
 *      `list[str]`. So ANY text in that box made Validate answer
 *      422 "command: Input should be a valid list" -- and the placeholder shipped in
 *      index.html, `bash /work/run.sh`, was itself a failing input. The only submittable state
 *      of the screen was an empty Command box running the image's own ENTRYPOINT.
 *   2. The Result path box sent `result_path`, a field the submit schema does not have.
 *      Pydantic ignores unknown fields, so the value vanished with no error at all: a user
 *      typed a bucket and their results went somewhere else. /v1/explain says plainly
 *      "Where the output goes ... There is no field for any of them."
 *
 * Neither is visible to the server's own tests, which start from a valid body, nor to a reader
 * of app.js alone, because being wrong requires knowing the server's types. What catches this
 * class of defect is asserting the SHAPE OF THE REQUEST the screen builds.
 *
 * HOW IT RUNS THE REAL CODE. app.js is a browser script with top-level statements that bind
 * handlers to elements, so it cannot be required into node. So the two functions under test are
 * EXTRACTED FROM THE SHIPPED FILE BY NAME and evaluated against a stub document. Nothing is
 * re-implemented here: if this file passes, the function in the deployed app.js passed.
 *
 * Run: node ui/submit_form_test.js      (no browser, no network, no login)
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const failures = [];

function check(condition, description) {
  console.log((condition ? "  ok    " : "  FAIL  ") + description);
  if (!condition) failures.push(description);
}

/* Pull one function out of app.js by name, brace-counting to its end. Deliberately blunt: a
 * rename in app.js makes this throw rather than silently test nothing. */
function extract(name) {
  const at = SRC.indexOf(`function ${name}(`);
  if (at < 0) throw new Error(`app.js has no function ${name} -- was it renamed?`);
  let depth = 0;
  for (let j = SRC.indexOf("{", at); j < SRC.length; j++) {
    if (SRC[j] === "{") depth++;
    else if (SRC[j] === "}" && --depth === 0) return SRC.slice(at, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

// ---------------------------------------------------------------------------------------------
// The stub document. Only what these two functions touch: fields by id, the vendor checkboxes,
// and the mode select's options.
// ---------------------------------------------------------------------------------------------
const fields = {};
const modeOptions = [{ value: "" }, { value: "cheapest" }, { value: "compare" }];
const vendorBoxes = [
  { dataset: { vendor: "aws" }, checked: false },
  { dataset: { vendor: "runpod" }, checked: false },
  { dataset: { vendor: "shadeform" }, checked: false },
  { dataset: { vendor: "gcp", priced: "" }, checked: false },
  { dataset: { vendor: "azure", priced: "" }, checked: false },
  { dataset: { vendor: "lambda", priced: "" }, checked: false },
  { dataset: { vendor: "nebius", priced: "" }, checked: false },
];

global.document = {
  getElementById: (id) => {
    if (!fields[id]) {
      fields[id] = { value: "", innerHTML: "" };
      if (id === "f-mode") fields[id].options = modeOptions;
    }
    return fields[id];
  },
  querySelectorAll: (selector) =>
    selector.includes("data-priced")
      ? vendorBoxes.filter((b) => "priced" in b.dataset)
      : vendorBoxes,
};
global.$ = (id) => document.getElementById(id);
global.note = (kind, text) => `[${kind}] ${text}`;

/* WRAPPED IN PARENTHESES AND ASSIGNED, rather than `eval(extract(...))` on its own. This file is
 * strict mode, and a bare eval'd function DECLARATION is scoped to the eval call -- it never
 * reaches the module, and the first line that calls it dies with "readForm is not defined".
 * Parenthesised, the same text is an EXPRESSION that evaluates to the function itself. */
const readForm = eval(`(${extract("readForm")})`);
const vendorRules = eval(`(${extract("vendorRules")})`);
const parseCompare = eval(`(${extract("parseCompare")})`);

function setForm(o) {
  $("f-name").value = o.name ?? "";
  $("f-image").value = o.image ?? "";
  $("f-command").value = o.command ?? "";
  $("f-parallelism").value = o.parallelism ?? 1;
  $("f-capacity").value = o.capacity ?? "spot";
  $("f-gpu").value = o.gpu ?? "";
  $("f-env").value = o.env ?? "";
  $("f-mode").value = o.mode ?? "";
  $("f-gpucount").value = o.gpucount ?? "";
  $("f-regions").value = o.regions ?? "";
  vendorBoxes.forEach((b) => {
    b.checked = (o.vendors || []).includes(b.dataset.vendor);
  });
  modeOptions.forEach((opt) => {
    opt.disabled = false;
  });
}

const IMAGE = "runpod/pytorch:torch291-cu1281";

// ---------------------------------------------------------------------------------------------
console.log("the command box, which was the one that made the screen unusable");

setForm({ image: IMAGE, command: "bash /work/run.sh" });
let body = readForm();
check(
  !("command" in body),
  "`command` is not sent at all. It is typed list[str] on the server and this box holds one " +
    "shell line, so sending the line as a string is the 422 this file exists to prevent"
);
check(
  JSON.stringify(body.args) === JSON.stringify(["bash", "-lc", "bash /work/run.sh"]),
  "the line goes out as args [bash, -lc, <line>], which is the shape /v1/explain's own " +
    "example uses"
);

setForm({ image: IMAGE, command: 'python -c "import torch; print(torch.__version__)"' });
body = readForm();
check(
  body.args.length === 3 &&
    body.args[2] === 'python -c "import torch; print(torch.__version__)"',
  "a quoted argument survives whole. Splitting the line on spaces instead would hand the " +
    "container four wrong tokens, which is why a shell gets the line rather than a splitter"
);

setForm({ image: IMAGE, command: "   " });
body = readForm();
check(
  !("args" in body) && !("command" in body),
  "a blank box sends neither, which leaves the image's own ENTRYPOINT in charge -- legitimate, " +
    "and the server's `something_to_run` validator allows it"
);

// ---------------------------------------------------------------------------------------------
console.log("\nthe result path box, which silently threw the value away");

setForm({ image: IMAGE });
body = readForm();
check(
  !("result_path" in body),
  "`result_path` is never sent. The submit schema has no such field, pydantic drops unknown " +
    "fields, and the user's typed bucket vanished with no error"
);
check(
  !SRC.includes('$("f-result")'),
  "and nothing in app.js still reads f-result -- the element is gone from index.html, so a " +
    "leftover read would throw and stop every field after it (the Clear button did exactly this)"
);

// ---------------------------------------------------------------------------------------------
console.log("\nvendors: the three cases the CRD has always supported and the screen could not say");

setForm({ image: IMAGE, vendors: ["aws"] });
check(JSON.stringify(readForm().vendors) === '["aws"]', "aws alone");

setForm({ image: IMAGE, vendors: ["runpod"] });
check(JSON.stringify(readForm().vendors) === '["runpod"]', "runpod alone");

setForm({ image: IMAGE });
body = readForm();
check(
  !("vendors" in body) && !("placement_mode" in body),
  "nothing checked sends neither field, which is 'no restriction' and is byte-for-byte what " +
    "every job did before these boxes existed"
);

setForm({ image: IMAGE, vendors: ["aws", "runpod"], mode: "cheapest" });
body = readForm();
check(
  JSON.stringify(body.vendors) === '["aws","runpod"]' && body.placement_mode === "cheapest",
  "two vendors and a mode go out together -- naming two on its own only lengthens the walk; " +
    "the mode is what turns it into a price comparison"
);

setForm({ image: IMAGE, vendors: ["aws", "runpod", "gcp", "azure", "lambda", "nebius"], mode: "compare" });
check(readForm().vendors.length === 6, "all six names are offered, not just the two that can run");

// ---------------------------------------------------------------------------------------------
console.log("\nthe price-only vendors, which can be ranked and never rented");

setForm({ image: IMAGE, vendors: ["gcp"], mode: "cheapest" });
vendorRules();
check(
  $("f-mode").value === "compare",
  "checking gcp forces the mode to compare. Under cheapest or ordered it can WIN the walk and " +
    "then fail at the actuator, because no actuator here understands its machine names"
);
check(
  modeOptions.filter((o) => o.disabled).map((o) => o.value).join(",") === ",cheapest",
  "and the other two options are disabled, so the screen never offers a checkbox whose only " +
    "outcome is a failed job"
);
check(
  $("f-mode-note").innerHTML.includes("priced but not rented"),
  "with a note saying why, rather than a select that moves on its own"
);

setForm({ image: IMAGE, vendors: ["aws"], mode: "" });
vendorRules();
check(
  $("f-mode").value === "" && modeOptions.every((o) => !o.disabled),
  "unchecking it gives every mode back. The forcing is a consequence of the choice, not a " +
    "one-way door"
);

setForm({ image: IMAGE, vendors: ["aws"], mode: "compare" });
vendorRules();
check(
  $("f-mode-note").innerHTML.includes("STOPS"),
  "and choosing compare on its own still says out loud that nothing is bought -- the one mode " +
    "where the job ends without the workload ever running"
);

// ---------------------------------------------------------------------------------------------
console.log("\nRun again, which used to drop what this screen now sends");

const rerun = SRC.slice(SRC.indexOf('$("d-again").onclick'));
const rerunBody = rerun.slice(0, rerun.indexOf("\n};"));
check(
  rerunBody.includes("sp.args"),
  "it reads sp.args. Reading `command` alone meant a job submitted from this screen came back " +
    "with no command at all, and the rerun ran the image's entrypoint instead"
);
check(
  rerunBody.includes("g.vramGB"),
  "it looks at gpus.vramGB. The server writes EITHER name OR vramGB and never both, so " +
    "reading name alone made a 48 GB ask come back as an empty box"
);
check(
  rerunBody.includes("pl.vendors") && rerunBody.includes("pl.mode"),
  "and it carries the placement back, which did not exist in the object before 2026-09-08"
);

// ---------------------------------------------------------------------------------------------
// ---------------------------------------------------------------------------------------------
console.log("\nthe script box, and the four checks that could never run");

setForm({ image: IMAGE, command: "set -euo pipefail\npython train.py\npython eval.py" });
body = readForm();
check(
  body.script === "set -euo pipefail\npython train.py\npython eval.py",
  "the same text is sent as `script` too. Four of validate's checks read `script` and nothing " +
    "else -- the adapter-path pair, the exit trap, the two length caps, the TRL patch -- so " +
    "sending only args meant those four could never run from this screen, whatever was pasted"
);
check(
  JSON.stringify(body.args) ===
    JSON.stringify(["bash", "-lc", "set -euo pipefail\npython train.py\npython eval.py"]),
  "and the same text still runs, newlines and all: a whole run.sh in one args element is what " +
    "`bash -lc` reads, so no file has to be written anywhere first"
);

setForm({ image: IMAGE });
body = readForm();
check(
  !("script" in body) && !("args" in body),
  "an empty box sends neither, so a job that means to use the image's own entrypoint still can"
);

// ---------------------------------------------------------------------------------------------
console.log("\nGPUs per pod, which six of the fourteen cards need");

setForm({ image: IMAGE, gpu: "A100-80GB", gpucount: 8 });
body = readForm();
check(
  body.gpu && body.gpu.count === 8,
  "the count reaches the request. validate refuses a card that is sold only as a whole 8-GPU " +
    "machine when the count is 1 (`gpu_count == 1 && !sold_singly`), so without this box six " +
    "of the fourteen options could be selected and never submitted"
);

setForm({ image: IMAGE, gpu: "L4" });
check(readForm().gpu.count === 1, "and it defaults to 1, which is every job that does not pack");

setForm({ image: IMAGE, gpucount: 4 });
check(
  !("gpu" in readForm()),
  "a count with no GPU chosen sends no gpu block at all -- 'let the server recommend one' has " +
    "no count to carry, and inventing one would pin a card the user did not pick"
);

console.log("\nthe compare panel, reading the operator's own sentence");

/* The real shape, from internal/controller/placement.go's `mode == placementModeCompare`
   return: "winner %s %s %s; runner-up %s; margin %s; %d of %d candidate(s) answered (%w)". */
const REAL =
  "winner runpod $1.590/hr buys 1 machine; runner-up aws $2.160/hr; margin 26.4%; " +
  "2 of 3 candidate(s) answered (mode=compare: the comparison IS the job, so nothing was bought)";
let c = parseCompare(REAL);
check(c.winner === "runpod $1.590/hr buys 1 machine", "the winner comes out whole, price and all");
check(c.runnerUp === "aws $2.160/hr", "so does the runner-up");
check(c.margin === "26.4%", "and the margin");
check(c.answered === "2 of 3 candidate(s) answered", "and how many of the candidates answered");
check(c.raw === REAL, "and the sentence itself is kept, because the parse is only as good as a " +
                      "format nobody promised us");

/* truncateMsg cuts status.message at 300 characters, so a long sentence really can lose its
   tail. Every part is optional and a missing one must not become an empty row. */
c = parseCompare("winner aws $2.160/hr buys 2 machines; runner-up");
check(c.winner === "aws $2.160/hr buys 2 machines",
      "a sentence cut short still yields the winner -- 300 characters is the ceiling " +
      "truncateMsg imposes and the tail is what it takes");
check(c.runnerUp === "" && c.margin === "" && c.answered === "",
      "and the parts that were cut come back empty rather than as guesses");

c = parseCompare("placement failed: no candidate answered");
check(!c.winner && !c.runnerUp && c.raw === "placement failed: no candidate answered",
      "a message this does not recognise yields nothing parsed and the text intact, which " +
      "leaves the reader exactly where they were before the panel existed");

c = parseCompare(undefined);
check(c.raw === "" && !c.winner, "and an absent message does not throw");

// ---------------------------------------------------------------------------------------------
// DDPSRUN-REGIONS. Blank is not "anywhere", and the request has to say so.
//
// WHY THIS IS THE MOST EXPENSIVE FIELD ON THE FORM TO GET WRONG. PACSrun gives an AWS ask that
// names no region exactly ONE region -- the operator's default (placement.go:376,
// PACSRUN-AWS-ONE-REGION). Until 2026-09-08 the screen sent nothing at all, so every job it ever
// submitted ran in us-west-2 and no one could ask otherwise. The H100 is $6.88/hour there and
// $8.60 in ap-northeast-1.
// ---------------------------------------------------------------------------------------------
setForm({ image: IMAGE, regions: "" });
check(readForm().regions === undefined,
      "an empty Regions box sends NO regions key, which the server reads as the operator's one " +
      "default -- not as a request to search everywhere");

setForm({ image: IMAGE, regions: "aws/us-east-1" });
check(JSON.stringify(readForm().regions) === '["aws/us-east-1"]',
      "one region is sent as a one-element list");

setForm({ image: IMAGE, regions: "aws/us-east-1, aws/ap-northeast-2  aws/eu-west-1" });
check(JSON.stringify(readForm().regions) ===
        '["aws/us-east-1","aws/ap-northeast-2","aws/eu-west-1"]',
      "commas and stray whitespace both separate, because a person typing three regions will " +
      "use whichever they think of first");

setForm({ image: IMAGE, regions: "  ,, aws/us-east-1 ,, " });
check(JSON.stringify(readForm().regions) === '["aws/us-east-1"]',
      "empty fragments are dropped rather than sent as \"\", which the server would have to " +
      "reject as a region name");

setForm({ image: IMAGE, regions: "AWS/US-East-1" });
check(JSON.stringify(readForm().regions) === '["AWS/US-East-1"]',
      "the text is sent VERBATIM and not normalised -- PACSrun compares region names exactly, " +
      "so a silent lowercasing would hide the one mistake the server can name precisely");

setForm({ image: IMAGE, regions: "" });

// ---------------------------------------------------------------------------------------------
// DDPSRUN-REGISTER. The address on the first-time visitor's screen.
//
// WHY THIS IS TESTED AT ALL, given it only fills a label: getting it wrong produces a screen
// that says "Signed in as an address we cannot read" to somebody whose sign-in just worked,
// which is precisely the "is this broken or am I not allowed in" confusion the screen exists to
// end. And the decoding is not trivial -- an id_token's payload is base64URL, not base64, so
// a bare atob throws on any token whose payload happens to contain - or _.
// ---------------------------------------------------------------------------------------------
const emailInToken = eval(`(${extract("emailInToken")})`);

// A token shaped exactly as Cognito's is: three dot-separated base64url segments. The payload
// is built here rather than pasted so the test carries no real token.
function fakeToken(payload) {
  const b64url = (obj) => Buffer.from(JSON.stringify(obj)).toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64url({ alg: "RS256" })}.${b64url(payload)}.not-a-real-signature`;
}

check(emailInToken(fakeToken({ email: "newcomer@example.ac.kr" })) === "newcomer@example.ac.kr",
      "the newcomer screen reads the address out of the browser's own id_token");

check(emailInToken(fakeToken({ sub: "x" })) === "",
      "a token with no email claim yields an empty string rather than undefined, so the label " +
      "falls back to its own wording instead of printing 'undefined'");

check(emailInToken("ddpsrun-static-token-not-a-jwt") === "",
      "a static token is not a JWT and must not throw here -- it has no email to show and the " +
      "person holding one is registered anyway");

check(emailInToken("") === "" && emailInToken(null) === "" && emailInToken(undefined) === "",
      "and neither does an absent credential");

check(emailInToken("a.!!!not-base64!!!.c") === "",
      "an unparseable payload is caught, because this runs before any screen is drawn and a " +
      "throw here would leave the page on 'Checking sign-in...' forever");

// The non-ASCII case is why the decode goes through decodeURIComponent: atob yields BYTES, and
// reading them as characters mangles any address that is not plain ASCII.
check(emailInToken(fakeToken({ email: "\uc5f0\uad6c\uc6d0@example.ac.kr" })) === "\uc5f0\uad6c\uc6d0@example.ac.kr",
      "a non-ASCII address survives the base64 decode");

// ---------------------------------------------------------------------------------------------
// DDPSRUN-UI-STALE-PANELS. A source check, not a behaviour one: drawMetrics returns early on a
// 404 without touching its panels, so the reset has to happen in drawDetail. On 2026-09-09
// `c-iter2-base` -- a job with no container, whose own facts row read "GPU: not yet known" --
// showed "360 samples, 09-04 16:39 ~ 09-04 22:39" under it, four days of another job's readings
// left behind by the previous render.
const detail = SRC.slice(SRC.indexOf("async function drawDetail"),
                         SRC.indexOf("const fact = (k, v) =>"));

check(detail.includes('$("d-gpu-panel").hidden = true'),
      "drawDetail hides the GPU panel on open, so a job with no container cannot show the " +
      "previous job's samples");

check(detail.includes('$("d-progress-panel").hidden = true'),
      "and the Progress panel with it -- the same render left a progress bar behind too");

check(detail.indexOf('$("d-gpu-panel").hidden = true') < detail.indexOf("drawMetrics(jobId, job)"),
      "and it happens BEFORE drawMetrics is asked, because that function's catch cannot");

// The reset must NOT be inside the poll: it runs every 5 seconds, and hiding there would blank
// a running job's chart on one transient 502.
const perOpen = detail.slice(0, detail.indexOf("poll.every("));
check(perOpen.includes('$("d-gpu-panel").hidden = true'),
      "the reset is once per open, not once per poll");

// ---------------------------------------------------------------------------------------------
// The per-card GPU table. A behaviour check on the real function, pulled out of app.js: it is
// pure, so it needs no document at all -- only CARD_COLORS, which it closes over.
//
// WHAT THIS PINS. The "Peak utilisation" column must come from the card's own
// peak_utilization_percent and NOT from the utilisation inside `peak`, which is the
// highest-MEMORY sample and carries whatever the card was doing at that instant. The numbers
// below are job-66b46719b854's real ones, read from /v1/jobs/job-66b46719b854/metrics on
// 2026-09-11: four A100s that each reached 77,631 MiB, whose memory-peak samples read 0, 3, 1
// and 95 per cent while every card's true peak was 99 or 100.
// ---------------------------------------------------------------------------------------------
const CARD_COLORS = ["var(--accent)", "var(--run)", "var(--ok)", "var(--bad)"];
const cardTable = eval(`(${extract("cardTable")})`);

const REAL_CARDS = [
  { gpu_index: 0, peak: { memory_used_mib: 77631, memory_total_mib: 81920, memory_percent: 94.8,
                          utilization_percent: 0 },
    peak_utilization_percent: 100.0, avg_utilization_percent: 84.6, series: new Array(393) },
  { gpu_index: 1, peak: { memory_used_mib: 77631, memory_total_mib: 81920, memory_percent: 94.8,
                          utilization_percent: 3 },
    peak_utilization_percent: 100.0, avg_utilization_percent: 78.0, series: new Array(393) },
  { gpu_index: 2, peak: { memory_used_mib: 77631, memory_total_mib: 81920, memory_percent: 94.8,
                          utilization_percent: 1 },
    peak_utilization_percent: 100.0, avg_utilization_percent: 80.1, series: new Array(393) },
  { gpu_index: 3, peak: { memory_used_mib: 77211, memory_total_mib: 81920, memory_percent: 94.3,
                          utilization_percent: 95 },
    peak_utilization_percent: 99.0, avg_utilization_percent: 37.8, series: new Array(393) },
];

const table = cardTable(REAL_CARDS);
const cells = [...table.matchAll(/<td class="num">([^<]*)<\/td>/g)].map((m) => m[1]);
// Four columns of numbers per row, in header order: peak memory, peak utilisation,
// average utilisation, samples.
const peakUtil = [cells[1], cells[5], cells[9], cells[13]];

check(JSON.stringify(peakUtil) === JSON.stringify(["100%", "100%", "100%", "99%"]),
      "the per-card table's Peak utilisation is the card's real maximum, not the utilisation "
      + "of its highest-memory sample (which read 0%, 3%, 1%, 95% on job-66b46719b854)");

check(!table.includes("Utilisation at peak"),
      "and the heading no longer says 'Utilisation at peak', which is what made a reader take "
      + "the memory-peak sample's number for the highest utilisation");

check([cells[2], cells[6], cells[10], cells[14]].join(",") === "84.6%,78%,80.1%,37.8%",
      "Average utilisation still comes from the card's own mean");

check(cardTable([{ gpu_index: 0, peak: null, latest: null, series: [] }]).includes("<td class=\"num\">-</td>"),
      "a card with no reading yet prints '-' rather than throwing");

// The Samples column. `series` is thinned to at most 400 points for the chart, so counting it
// reported 393 for a card that printed 785 (same job, same report). sample_count is the
// readings actually taken, and it is what the mean and the peak above are computed over.
const counted = cardTable([{ gpu_index: 0, peak: { memory_used_mib: 1, memory_total_mib: 2,
                                                   memory_percent: 50, utilization_percent: 9 },
                             peak_utilization_percent: 100, avg_utilization_percent: 84.6,
                             sample_count: 785, series: new Array(393) }]);
check(counted.includes(">785<") && !counted.includes(">393<"),
      "the Samples column counts the readings taken (785), not the chart points left after "
      + "downsampling (393)");

// An older server sends no sample_count, and then the thinned length is the only number there
// is -- better than a blank column.
const older = cardTable([{ gpu_index: 0, peak: null, latest: null, series: new Array(12) }]);
check(older.includes(">12<"),
      "and a server too old to send sample_count still fills the column from the series");

// ---------------------------------------------------------------------------------------------
// The Getting started card. A source check on index.html, because every line there is a command
// somebody copies and a line that is only most of a command fails quietly: `hyperun login` on
// its own exits 2 (`--server` is argparse-required), and `/plugin marketplace add` registers a
// marketplace without installing anything. Both shipped that way until 2026-09-11.
// ---------------------------------------------------------------------------------------------
const HTML = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const started = HTML.slice(HTML.indexOf("Getting started"), HTML.indexOf("2. Jobs"));

check(started.includes("/plugin install hyperun"),
      "Getting started tells the reader to INSTALL the plugin, not only to add the marketplace "
      + "(README.md prints both commands and this card printed one)");

check(!/>\s*hyperun login\s*</.test(started),
      "and the sign-in line is not a bare `hyperun login`, which exits 2 because --server is "
      + "required");

check(SRC.includes("`hyperun login --server ${store.server}`"),
      "app.js fills that line with this deployment's own address rather than a placeholder");

// ---------------------------------------------------------------------------------------------
// DDPSRUN-SCRIPT-REUSE. The submit screen offers the scripts already submitted. Source checks:
// the function is small and its one rule is what it must NOT touch.
// ---------------------------------------------------------------------------------------------
const reuse = SRC.slice(SRC.indexOf("async function drawScriptReuse"),
                        SRC.indexOf('$("f-image-toggle").onclick'));

check(HTML.includes('id="f-script-reuse-toggle"') && HTML.includes('id="f-script-reuse"'),
      "the submit screen has the reuse control beside the Script box, not only on the Scripts "
      + "screen the person would have to leave the half-filled form to reach");

check(reuse.includes('$("f-command").value = row.script'),
      "clicking a script writes it straight into the Script box");

check(!reuse.includes('$("f-name")'),
      "and does NOT copy the name, which would collide with the job the script came from");

check(!/\$\("f-(image|gpu|capacity|mode)"\)\.value\s*=/.test(reuse),
      "nor the image, GPU, capacity type or mode -- those are the new job's own decisions");

check(reuse.includes('call("/v1/scripts")'),
      "the list is the same /v1/scripts the Scripts screen reads, so the two cannot disagree");

// ---------------------------------------------------------------------------------------------
// DDPSRUN-IMAGES-TRIM and DDPSRUN-TRAINING-SIZE. Both are about what the form shows first, so
// both are source checks over the shipped files.
// ---------------------------------------------------------------------------------------------
const picker = SRC.slice(SRC.indexOf("function renderImagePicker"),
                         SRC.indexOf('$("f-image-search").oninput'));

check(picker.includes("addrs.slice(0, TAGS_SHOWN)") && picker.includes("more-tags"),
      "the image picker draws the newest few tags per repository with the rest behind a button, "
      + "instead of 55 sha-named buttons at once");

check(picker.includes("String(r.repository).toLowerCase().includes(needle)"),
      "and the search box filters by repository name, which is what 12 unrelated projects in "
      + "the list made necessary");

check(picker.includes("The box above still takes any address"),
      "a filter that matches nothing says the box is still free text, so a public image stays "
      + "reachable");

check(HTML.includes('id="f-size-box" hidden'),
      "the five optional Training size boxes start folded");

check(SRC.includes("value${filled === 1 ? \"\" : \"s\"} set and still sent"),
      "and a value typed then folded away is still announced, because the form still sends it");

// ---------------------------------------------------------------------------------------------
// DDPSRUN-UI-STALE-TAB. A behaviour check on the real function: a tab that never reloads runs
// old code forever and looks normal doing it, which cost two exchanges on 2026-09-11.
// ---------------------------------------------------------------------------------------------
{
  const bars = {};
  const stubDoc = { getElementById: (id) => (bars[id] = bars[id] || { hidden: true, innerHTML: "" }) };
  const ctx = {
    $: stubDoc.getElementById,
    esc: (v) => String(v ?? ""),
    RUNNING_VERSION: "",
  };
  const checkStale = new Function("$", "esc", "RUNNING_VERSION",
    `${extract("checkStale")}; return checkStale;`);

  const run = (running, deployed) => {
    bars.stale = { hidden: true, innerHTML: "" };
    checkStale(ctx.$, ctx.esc, running)(deployed);
    return bars.stale;
  };

  check(run("aaaaaaaaaaaa", "bbbbbbbbbbbb").hidden === false,
        "a page running an older build than the deployed one says so in a bar that does not "
        + "fade -- the whole failure is that a stale tab looks normal");

  check(run("aaaaaaaaaaaa", "aaaaaaaaaaaa").hidden === true,
        "and a page running the deployed build says nothing");

  check(run("", "bbbbbbbbbbbb").hidden === true && run("aaaaaaaaaaaa", "").hidden === true,
        "'I cannot tell' is not shown as 'you are out of date': a local file with no ?v=, or a "
        + "deployment older than the version field, stays quiet");

  check(run("aaaaaaaaaaaa", "bbbbbbbbbbbb").innerHTML.includes("aaaaaaa")
        && run("aaaaaaaaaaaa", "bbbbbbbbbbbb").innerHTML.includes("bbbbbbb"),
        "the bar names both builds, so the reader can tell a support answer from their own screen");
}

check(SRC.includes('setInterval(pollDeployedVersion'),
      "and the check runs again while the tab stays open, not only at load -- a tab open "
      + "overnight is exactly the case that breaks");

check(/url\.searchParams\.set\("v"/.test(SRC),
      "the Reload button changes the URL rather than calling location.reload(), which some "
      + "browsers serve from cache");

// ---------------------------------------------------------------------------------------------
// The two defaults the markup ships, and the vendor list it offers. Source checks on
// index.html, because both are decided before any handler runs.
// ---------------------------------------------------------------------------------------------

// DDPSRUN-CAPACITY-DEFAULT. Neither the server nor the CLI has a default -- capacity_type is
// None in SubmitRequest and --capacity-type is one you must pass -- so the box's default is
// this screen's own decision, and a reclaimed machine costs a run already hours in.
check(/<option value="on-demand" selected>/.test(HTML),
      "the capacity box defaults to on-demand, so accepting the default cannot lose a long run "
      + "to a reclaim");

check(SRC.includes('$("f-capacity").value = "on-demand"'),
      "and Clear lands on that same default, rather than silently putting the form back on spot");

// shadeform joined models.RUNNABLE_VENDORS on 2026-09-10 and has completed live runs; this row
// still offered aws and runpod only, so a vendor that sells to us could not be asked for.
check(/data-vendor="shadeform"(?![^>]*data-priced)/.test(HTML),
      "shadeform is offered in the runnable vendor row, not the price-only one");

{
  const runnable = HTML.slice(HTML.indexOf('id="f-vendors"'), HTML.indexOf('id="f-vendors-priced"'));
  const offered = [...runnable.matchAll(/data-vendor="([a-z]+)"/g)].map((m) => m[1]);
  check(JSON.stringify(offered) === JSON.stringify(["aws", "runpod", "shadeform"]),
        "and the runnable row is exactly models.RUNNABLE_VENDORS -- aws, runpod, shadeform");
}

// ---------------------------------------------------------------------------------------------
// DDPSRUN-SHELL-CWD. The prompt has to say where the next line will run, and the marker that
// carries that must never reach the transcript. Behaviour checks on the real functions.
// ---------------------------------------------------------------------------------------------
{
  const CWD_MARK = "__DDPSRUN_CWD__";
  // takeCwd closes over CWD_MARK, so the marker is passed in rather than redefined -- the value
  // under test is the one app.js ships.
  const MARK = SRC.match(/const CWD_MARK = "([^"]+)"/)[1];
  const takeCwd = new Function("CWD_MARK", `${extract("takeCwd")}; return takeCwd;`)(MARK);
  check(MARK === CWD_MARK, "the marker this test uses is the one app.js defines");

  const [clean, cwd] = takeCwd(`total 4\ndrwxr-xr-x work\n\n${CWD_MARK}/workspace\n`);
  check(cwd === "/workspace", "the directory comes back in the same round trip as the command");
  check(!clean.includes(CWD_MARK),
        "and the marker never reaches the transcript -- it is ours, not the workload's");
  check(clean.startsWith("total 4\ndrwxr-xr-x work"),
        "the command's own output is untouched in front of it");

  // One response can carry output the caller had not read yet, so an earlier line's marker may
  // still be in the buffer. The last one is where the shell is NOW.
  const [, latest] = takeCwd(`${CWD_MARK}/old\nsomething\n${CWD_MARK}/workspace/data\n`);
  check(latest === "/workspace/data", "with two markers buffered, the last one wins");

  check(takeCwd("no marker here")[1] === "",
        "and output with no marker yields no directory rather than a wrong one");
}

{
  // withCwdProbe. Every expectation below was measured against the live session on 2026-09-12.
  const MARK2 = SRC.match(/const CWD_MARK = "([^"]+)"/)[1];
  const at = SRC.indexOf("function withCwdProbe");
  let depth = 0, j = SRC.indexOf("{", at);
  for (; j < SRC.length; j++) { if (SRC[j] === "{") depth++; else if (SRC[j] === "}" && --depth === 0) break; }
  const probe = new Function("CWD_MARK", `${SRC.slice(at, j + 1)}; return withCwdProbe;`)(MARK2);

  check(probe("ls").startsWith("ls; printf"),
        "the directory probe rides on the same line with `; ` -- a newline was measured to run "
        + "the first line only, the driver drops the rest");

  check(probe("echo a;") === probe("echo a"),
        "a trailing semicolon is dropped, because `echo a;; printf` is a syntax error");

  check(probe("sleep 0 &") === "sleep 0 &",
        "a line ending in a lone & is sent alone -- `&; printf` is a syntax error, and "
        + "backgrounding something does not move the directory anyway");

  check(probe("a && b").includes("printf"),
        "but && is not that case and still gets the probe");
}

check(SRC.includes('shellUI.cwd ? `${shellUI.jobKey}:${shellUI.cwd}$`'),
      "the prompt is job and directory once the directory is known, so `cd` moves it");

check(/shellUI\.cwd = "";/.test(SRC),
      "and a reopened session forgets the directory, because a new shell starts where the image "
      + "does and not where the old one was");

// The layout is the one that was already there and the reporter asked to keep: transcript,
// prompt, input, Run button. Enter runs the line too, and always did.
check(SRC.includes('$("d-shell-send").addEventListener("click", submit)'),
      "the Run button is still wired -- only the prompt changed");

// ---------------------------------------------------------------------------------------------
console.log();
if (failures.length) {
  console.log(`FAILED (${failures.length}):`);
  failures.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
console.log(
  "the New job screen sends a body the server accepts, says which vendors may sell the " +
    "machine, throws nothing away in silence, and a first-time visitor is told which address " +
    "they signed in as"
);
