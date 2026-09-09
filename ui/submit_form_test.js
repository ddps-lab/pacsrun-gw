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
