/* learning_panel_test.js -- what the Learning panel draws, and what it does NOT spend.
 *
 * WHY THIS FILE EXISTS. On 2026-09-15 a job finished `Succeeded` after 9 h 44 m on four A100s
 * at $6.36/hour with a perfect progress bar, and one of its nine trainings had run its
 * objective from an average of 2.850 down to 2.140 -- 25% -- while every log it uploaded
 * contained the word `loss` exactly zero times. `Progress` says how far a run has got and
 * `GPU` says the cards are busy; neither answers "is it learning". This panel does, and these
 * are the assertions that say it answers it correctly.
 *
 * THE TWO THINGS WORTH PINNING:
 *
 *   1. NINE TRAININGS STAY NINE ROWS. Averaged together they look fine, which is exactly how
 *      the bad one hid. The fixture below has one falling series and one rising one and both
 *      have to appear with their own numbers.
 *   2. OPENING A JOB MUST NOT SPEND MONEY. The verdict is arithmetic and free; the SENTENCE is
 *      a model call at about $0.0002. So `explain=true` must not be sent until somebody presses
 *      the button, and `test_the_model_is_not_called_unless_it_is_asked_for` in the server
 *      suite pins the other half of that.
 *
 * HOW IT RUNS THE REAL CODE. Same technique as submit_form_test.js beside it: the functions are
 * EXTRACTED FROM THE SHIPPED app.js BY NAME and evaluated against a stub document. Nothing is
 * re-implemented, so if this passes, the function in the deployed file passed.
 *
 * Run: node ui/learning_panel_test.js      (no browser, no network, no login)
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

/* Blunt on purpose: a rename in app.js makes this throw rather than silently test nothing.
 *
 * `async` IS PART OF THE DECLARATION AND HAS TO COME WITH IT. Slicing from `function` alone
 * drops it, and the extracted text then fails to parse on its own `await` -- which reads as a
 * bug in app.js rather than in this extractor. */
function extract(name) {
  let at = SRC.indexOf(`function ${name}(`);
  if (at < 0) throw new Error(`app.js has no function ${name} -- was it renamed?`);
  if (SRC.slice(at - 6, at) === "async ") at -= 6;
  let depth = 0;
  for (let j = SRC.indexOf("{", at); j < SRC.length; j++) {
    if (SRC[j] === "{") depth++;
    else if (SRC[j] === "}" && --depth === 0) return SRC.slice(at, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

// --------------------------------------------------------------------------------- the stubs

const nodes = {};
global.document = {
  getElementById: (id) => (nodes[id] = nodes[id] || {
    id, innerHTML: "", textContent: "", hidden: false, disabled: false, onclick: null,
  }),
};

const asked = [];
let answer = null;

// `call` and the two helpers the extracted functions close over. Taken from app.js where they
// are one-liners, stubbed where they reach the network.
global.$ = (id) => document.getElementById(id);
global.esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
global.nsQuery = (lead) => "";
global.call = async (route) => {
  asked.push(route);
  if (!route.includes("/analysis")) throw new Error("unexpected route " + route);
  return {
    findings: answer.findings,
    explanation: route.includes("explain=true") ? "점수가 25% 떨어졌습니다." : "",
    checked: answer.checked,
    note: answer.note || "",
  };
};

/* The two word lists are `const NAME = [...]` spanning a line or two; this lifts the array
 * literal out of app.js so the test cannot drift from the words the screen actually uses. */
const extract_const = (name) => {
  const src = SRC.slice(SRC.indexOf(`const ${name} = `) + `const ${name} = `.length);
  return src.slice(0, src.indexOf("];") + 1);
};

/* WRAPPED IN PARENTHESES AND ASSIGNED, rather than `eval(extract(...))` on its own -- the same
 * note submit_form_test.js carries beside it. This file is "use strict", and a declaration
 * inside a strict-mode eval is local to that eval, so the bare form defines a function nobody
 * outside can see and the test then fails with `drawLearning is not defined`. */
// ★ `betterDirection` AND ITS TWO WORD LISTS COME FIRST, because trendLabel calls it. A
// strict-mode eval keeps its declarations local, so each of these has to be assigned into this
// scope by hand -- pulling trendLabel alone gives `betterDirection is not defined` at the first
// row drawn.
const BETTER_UP = eval(extract_const("BETTER_UP"));
const BETTER_DOWN = eval(extract_const("BETTER_DOWN"));
const betterDirection = eval(`(${extract("betterDirection")})`);
const trendLabel = eval(`(${extract("trendLabel")})`);
const drawLearning = eval(`(${extract("drawLearning")})`);

// --------------------------------------------------------------------------------- the fixture
//
// Two trainings out of a job that ran nine. The first is the real one that went backwards; the
// second rose. A panel that reported one number for the job would have to be wrong about one of
// them.
const trend = (head, tail, ratio, nan) => ({
  slope: (tail - head) / 20, head, tail, change_ratio: ratio, has_nan: !!nan, window: 5,
});

const METRICS = {
  metric_series: [
    {
      name: "bank/adapters/AD/iter_1", step_key: "step", row_count: 20,
      first_step: 1, last_step: 20, fields: ["entropy", "kl", "score"], rows: [],
      trends: {
        score: trend(2.850, 2.140, -0.249),
        kl: trend(0.0, -5.7, null),
        entropy: trend(33.2, 38.0, 0.144),
      },
    },
    {
      name: "market/adapters/AD/iter_1", step_key: "step", row_count: 22,
      first_step: 1, last_step: 22, fields: ["score"], rows: [],
      trends: { score: trend(2.889, 3.124, 0.081) },
    },
  ],
};

// --------------------------------------------------------------------------------- the checks

(async () => {
  answer = {
    findings: [{
      rule: "regression", series: "bank/adapters/AD/iter_1", field: "score",
      change_ratio: -0.249,
      detail: "bank/adapters/AD/iter_1: score went the wrong way, 2.85 to 2.14 over 20 steps",
    }],
    checked: ["silence", "crash", "regression", "nan"],
  };

  await drawLearning("job-a24568ecfc16", METRICS, "?window_seconds=3600");

  check(nodes["d-learning-panel"].hidden === false, "the panel is shown when there are metrics");

  const table = nodes["d-learning"].innerHTML;
  check(table.includes("bank/adapters/AD/iter_1") && table.includes("market/adapters/AD/iter_1"),
        "both trainings appear, with their own names");
  check(table.includes("-24.9%"), "the training that went backwards shows its fall");
  check(table.includes("+8.1%"), "the training that improved shows its rise");
  check(table.includes("2.850") && table.includes("2.140"),
        "the first and last values are shown, not only the ratio");
  check(nodes["d-learning-note"].textContent.includes("2 trainings"),
        "the note counts the trainings rather than the job");

  check(nodes["d-learning-findings"].innerHTML.includes("went the wrong way"),
        "the finding the rules produced is shown");
  check(nodes["d-explain"].disabled === false, "the explain button is enabled when something fired");

  // ★ The money assertion. Drawing the panel asks /analysis, which is arithmetic and free.
  check(asked.some((r) => r.includes("/analysis")), "the rules are asked for");
  check(!asked.some((r) => r.includes("explain=true")),
        "★ no model call on load: opening a job costs nothing");

  await nodes["d-explain"].onclick();
  check(asked.some((r) => r.includes("explain=true")), "pressing the button asks for the sentence");
  check(nodes["d-explanation"].textContent.includes("25%"), "the sentence is shown");
  check(nodes["d-explain"].disabled === false, "the button is usable again afterwards");

  // ------------------------------------------------------------------ the quiet cases

  asked.length = 0;
  answer = { findings: [], checked: ["silence", "crash"], note: "this job has printed no training metrics" };
  await drawLearning("job-x", { metric_series: [] }, "");
  check(nodes["d-learning-panel"].hidden === true,
        "a job with no training numbers hides the panel rather than showing an empty one");
  check(asked.length === 0, "and asks nothing, so a CPU job costs no extra request");

  answer = { findings: [], checked: ["silence", "crash", "regression", "nan"], note: "" };
  await drawLearning("job-y", METRICS, "");
  check(nodes["d-learning-findings"].innerHTML.includes("Checks passed"),
        "a healthy run says which checks passed rather than showing nothing");
  // ★ THE BUTTON IS ON FOR A HEALTHY RUN TOO, SINCE 2026-09-16. It used to be off, and the
  // worry behind that was real -- a model asked to explain an empty findings list invents a
  // fault. The server answers that by using a DIFFERENT prompt when nothing is wrong, one
  // that says outright that the check found nothing and forbids making one up. Turning the
  // button off instead meant the numbers on most jobs could never be explained at all.
  check(nodes["d-explain"].disabled === false,
        "the explain button works on a healthy run too");
  check(nodes["d-explain"].textContent === "What has this run done?",
        "and it says which question it is going to answer")

  // ------------------------------------------------------------------ a broken number

  const NAN = { metric_series: [{
    name: "t", step_key: "step", row_count: 12, first_step: 1, last_step: 12,
    fields: ["loss"], rows: [], trends: { loss: trend(0, 0, null, true) },
  }] };
  answer = { findings: [{ rule: "nan", series: "t", field: "loss", detail: "t: loss contains NaN" }],
             checked: ["silence", "crash", "regression", "nan"] };
  await drawLearning("job-z", NAN, "");
  check(nodes["d-learning"].innerHTML.includes("NaN"),
        "a NaN is named in the table rather than shown as a number");

  // ★ THE CHANGE COLUMN HAS TO SAY WHICH WAY IS BETTER (2026-09-16). It used to read
  // `-24.9%` and stop, and a reader had to already know that down is good for `loss` and bad
  // for `score` before the number meant anything. This panel exists to be glanced at.
  check(trendLabel(trend(2.0, 1.0, -0.5, false), "train_loss").includes("better"),
        "loss going down is better");
  check(trendLabel(trend(1.0, 2.0, 1.0, false), "train_loss").includes("worse"),
        "loss going up is worse, and is marked as wrong");
  check(trendLabel(trend(2.85, 2.14, -0.249, false), "score").includes("worse"),
        "score going down is worse -- the real run this panel was built for");
  check(trendLabel(trend(0.1, 0.9, 8.0, false), "eval_acc").includes("better"),
        "accuracy going up is better");

  // ★★ AND IT MUST SAY NOTHING WHEN THE NAME SAYS NOTHING. `grad_norm` and `learning_rate`
  // have no better direction, and colouring them would be the screen inventing a verdict.
  for (const field of ["grad_norm", "learning_rate", "epoch", "step_time"]) {
    const label = trendLabel(trend(1.0, 2.0, 1.0, false), field);
    check(!label.includes("better") && !label.includes("worse") && !label.includes("wrong"),
          `${field} gets a number and no verdict, because its name does not say which way is good`);
  }

  console.log(failures.length ? `\n${failures.length} FAILED` : "\nall ok");
  process.exit(failures.length ? 1 : 0);
})();
