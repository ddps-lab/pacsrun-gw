/* job_machine_test.js -- the machine and the GPU as two lines (HYPERUN-INSTANCE-AND-GPU).
 *
 * WHY THIS FILE EXISTS. The job page printed the server's `gpu` field under the label "GPU",
 * and that field held the vendor's ONE name for what was rented. For a CPU job on AWS that is
 * an EC2 instance type, so the page read "GPU  t3.xlarge" (seen 2026-10-07 on job
 * job-1d6bcd94b948). The server now sends `instance`, `gpu_model` and `gpu_count`; this file
 * pins how the screen turns them into an Instance line and a GPU line, and that a job which
 * asked for no GPU gets no GPU line at all.
 *
 * HOW IT RUNS THE REAL CODE. The same way as submit_form_test.js: the two functions under
 * test are extracted from the shipped app.js by name and evaluated, so a pass here is a pass
 * of the deployed code. The wiring into the page and the tables is checked on the source,
 * because those functions need the whole DOM.
 *
 * Run: node ui/job_machine_test.js      (no browser, no network, no login)
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

const instanceText = new Function(`${extract("instanceText")}; return instanceText;`)();
const gpuText = new Function(`${extract("gpuText")}; return gpuText;`)();

console.log("instanceText: the machine, in the vendor's own words");
check(instanceText({ vendor: "aws", instance: "t3.xlarge", gpu: "t3.xlarge" }) === "t3.xlarge",
  "an AWS CPU job names its EC2 instance type");
check(instanceText({ vendor: "gcp", instance: "g2-standard-4", gpu: "g2-standard-4+L4:1" })
  === "g2-standard-4", "a GCP job names the machine type, not the attached card");
check(instanceText({ vendor: "runpod", instance: null, gpu: "NVIDIA L40S" }) === "RunPod pod",
  "a RunPod job says it rented a pod, and does not fall back to the GPU type in `gpu`");
check(instanceText({ vendor: null, instance: null, gpu: null }) === null,
  "nothing is named before a machine exists");
check(instanceText({ vendor: "aws", gpu: "t3.xlarge" }) === "t3.xlarge",
  "a gateway older than this screen sends no `instance`, and its `gpu` is the machine");

console.log("gpuText: model x count, only for a job that asked for a GPU");
check(gpuText({ gpu_model: null, gpu_count: null }, true) === null,
  "a CPU job has no GPU text");
check(gpuText({ gpu_model: "L4", gpu_count: 1 }, true) === "L4 × 1 per pod",
  "the job page says the count is per pod");
check(gpuText({ gpu_model: "A100-SXM4-80GB", gpu_count: 4 }, false) === "A100-SXM4-80GB × 4",
  "the jobs table keeps it short");
check(gpuText({ gpu_model: null, gpu_count: 2 }, true) === null,
  "an ask with no machine yet has no model to print");

console.log("wiring");
check(!/fact\("GPU",\s*job\.gpu\b/.test(SRC),
  "the job page no longer prints the old `gpu` field under the GPU label");
check(/fact\("Instance",\s*instanceText\(job\)/.test(SRC),
  "the job page has an Instance line");
check(/\.\.\.\(job\.gpu_count\s*\?\s*\[fact\("GPU",\s*gpuText\(job, true\)/.test(SRC),
  "the job page draws the GPU line only when the job asked for a GPU");
check(/jobsTable\(running\.slice\(0, 5\), \["name", "status", "elapsed", "instance"\]\)/.test(SRC),
  "Home's running list shows the machine");
check(/"elapsed", "instance", "gpu", "vendor"/.test(SRC),
  "the Jobs table has an Instance column and a GPU column");

if (failures.length) {
  console.log(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
