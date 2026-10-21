// Exercise dashboard request/session flows without a browser or live Plenx calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  const start = source.indexOf("async function refresh(");
  const end = source.indexOf('$("refresh").addEventListener', start);
  assert.ok(start >= 0 && end > start);
  let release;
  const slowHistory = new Promise(resolve => { release = resolve; });
  const checked = [];
  let firstRequest = true;
  const context = vm.createContext({
    accounts: [{id: "original", updated_at: "old"}], refreshing: false, wrapperPassword: "wrapper-test-password",
    creditsRefreshPending: false,
    jobsByAccount: new Map(), profilesByAccount: new Map(),
    accountIssues: new Map(), cooldowns: new Map(), ACTIVE: new Set(["queued"]),
    $: () => ({}), renderAccounts() {}, renderRequests() {}, notice() {},
    async refreshProfile(account) { checked.push(account.id); },
    async api() {
      if (firstRequest) { firstRequest = false; await slowHistory; }
      return {requests: []};
    },
  });
  vm.runInContext(source.slice(start, end), context);
  const running = context.refresh();
  context.accounts.push({id: "new-account", updated_at: "new"});
  await context.refresh({credits: true});
  release();
  await running;
  assert.ok(checked.includes("new-account"), "A forced refresh during an existing refresh must check the newly saved account");
  assert.equal(context.refreshing, false);
  console.log("Dashboard forced-refresh regression passed");

  let releaseBody;
  let bodyStarted;
  const reading = new Promise(resolve => { bodyStarted = resolve; });
  const responseBody = new Promise(resolve => { releaseBody = resolve; });
  const session = vm.createContext({
    wrapperPassword: "password", sessionVersion: 1, accounts: [], selected: "",
    async fetch() {
      return {status: 200, ok: true, async json() { bodyStarted(); return await responseBody; }};
    },
    selectAccount() {}, renderRequests() {},
  });
  const apiStart = source.indexOf("async function api(");
  const apiEnd = source.indexOf('$("generation-form").addEventListener', apiStart);
  vm.runInContext(source.slice(apiStart, apiEnd), session);
  const loadStart = source.indexOf("async function loadAccounts(");
  const loadEnd = source.indexOf("function lockDashboard(", loadStart);
  vm.runInContext(source.slice(loadStart, loadEnd), session);
  const loading = session.loadAccounts();
  await reading;
  session.wrapperPassword = "";
  session.sessionVersion += 1;
  releaseBody({accounts: [{id: "private-account", name: "Private account"}]});
  await assert.rejects(loading, /Session changed/);
  assert.equal(session.accounts.length, 0, "A response finishing after logout must not restore private account metadata");
  console.log("Dashboard logout-during-response regression passed");

  let legacyData = JSON.stringify([{id: "old-a", name: "A", token: "legacy-a"}, {id: "old-b", name: "B", token: "legacy-b"}]);
  const migration = vm.createContext({
    selected: "old-a", LEGACY_STORAGE_KEY: "old-accounts",
    localStorage: {
      getItem() { return legacyData; },
      setItem(key, value) { legacyData = value; },
      removeItem() { legacyData = null; },
    },
    async api(account, route, options) {
      if (JSON.parse(options.body).token === "legacy-b") throw new Error("server unavailable");
      return {id: "server-a"};
    },
    notice() {},
  });
  const migrationStart = source.indexOf("async function migrateLegacyAccounts(");
  const migrationEnd = source.indexOf('$("login-dialog").addEventListener', migrationStart);
  vm.runInContext(source.slice(migrationStart, migrationEnd), migration);
  await assert.rejects(migration.migrateLegacyAccounts(), /server unavailable/);
  assert.deepEqual(JSON.parse(legacyData), [{id: "old-b", name: "B", token: "legacy-b"}]);
  assert.equal(migration.selected, "server-a");
  console.log("Dashboard partial-migration recovery regression passed");

  const elements = new Map();
  const handlers = new Map();
  const authCalls = [];
  let setupSaved = false;
  const auth = vm.createContext({
    setupRequired: false, authStatusReady: false, wrapperPassword: "", sessionVersion: 0,
    $(id) {
      if (!elements.has(id)) elements.set(id, {value: "", close() {}, addEventListener(event, handler) { handlers.set(`${id}:${event}`, handler); }});
      return elements.get(id);
    },
    async fetch(route, options) {
      authCalls.push(route);
      if (route === "/auth/setup") {
        assert.deepEqual(JSON.parse(options.body), {password: "setup-test-password", confirm_password: "setup-test-password"});
        setupSaved = true;
      }
      return {ok: true, async json() { return {setup_required: !setupSaved}; }};
    },
    async loadAccounts() { assert.equal(auth.wrapperPassword, "setup-test-password"); },
    async migrateLegacyAccounts() {}, async refresh() {}, notice() {},
    lockDashboard() { auth.wrapperPassword = ""; },
  });
  const authStart = source.indexOf("function configureAuthMode(");
  const authEnd = source.indexOf("function notice(", authStart);
  vm.runInContext(source.slice(authStart, authEnd), auth);
  const loginStart = source.indexOf('$("login-dialog").addEventListener');
  const loginEnd = source.indexOf("function renderAccounts(", loginStart);
  vm.runInContext(source.slice(loginStart, loginEnd), auth);
  await auth.loadAuthStatus();
  assert.equal(elements.get("confirm-password-field").hidden, false);
  assert.equal(elements.get("login-submit").textContent, "Set password");
  elements.get("wrapper-password").value = "setup-test-password";
  elements.get("confirm-password").value = "different";
  const submitLogin = handlers.get("login-form:submit");
  await submitLogin({preventDefault() {}});
  assert.deepEqual(authCalls, ["/auth/status"], "Mismatched confirmation must not submit setup");
  assert.equal(elements.get("login-error").textContent, "Passwords do not match.");
  elements.get("confirm-password").value = "setup-test-password";
  await submitLogin({preventDefault() {}});
  assert.equal(setupSaved, true);
  assert.equal(auth.wrapperPassword, "setup-test-password", "Successful setup signs in using the chosen password");
  assert.equal(elements.get("confirm-password-field").hidden, true);
  assert.equal(elements.get("confirm-password").value, "");
  assert.equal(elements.get("wrapper-password").value, "");
  await auth.loadAuthStatus();
  assert.equal(elements.get("login-submit").textContent, "Sign in", "A later visit uses ordinary sign in");
  console.log("Dashboard first-visit setup and sign-in regression passed");

  const videoElements = new Map();
  const videoValues = {
    prompt: "a cat", "video-model": "omni-flash", "video-aspect": "16:9", "video-duration": "4",
    "video-resolution": "720", "server-id": "56947b53-029e-4751-bbca-b9de54f5d628", "workplace-id": "",
    "video-references": " https://images.plenxai.com/first.png \n\nhttps://example.test/second.png?frame=2&quality=100\r\n",
  };
  let videoSubmit;
  let submittedForm;
  const video = vm.createContext({
    FormData, accounts: [{id: "account", name: "Test account"}], selected: "account", mode: "video", submitting: false,
    $(id) {
      if (!videoElements.has(id)) videoElements.set(id, {value: videoValues[id] || "", addEventListener(event, handler) { videoSubmit = handler; }});
      return videoElements.get(id);
    },
    async api(account, route, options) {
      assert.equal(route, "/api/v1/veo/generate");
      submittedForm = options.body;
      return {status: "queued"};
    },
    notice() {}, setMode() {}, async refresh() {},
  });
  const formStart = source.indexOf('$("generation-form").addEventListener');
  const formEnd = source.indexOf("function mediaUrls(", formStart);
  vm.runInContext(source.slice(formStart, formEnd), video);
  await videoSubmit({preventDefault() {}});
  assert.equal(submittedForm.getAll("reference_images").length, 1);
  assert.deepEqual(JSON.parse(submittedForm.get("reference_images")), ["https://images.plenxai.com/first.png", "https://example.test/second.png?frame=2&quality=100"]);
  videoElements.get("video-references").value = " \n ";
  await videoSubmit({preventDefault() {}});
  assert.deepEqual(JSON.parse(submittedForm.get("reference_images")), []);
  console.log("Dashboard video reference-image multipart submission passed");

  const queueJobs = new Map();
  const waitingJob = {id: "local-request", task_id: null, status: "waiting", queue_position: 2};
  const queueView = vm.createContext({
    accounts: [{id: "account", updated_at: "current"}], wrapperPassword: "test", refreshing: false,
    creditsRefreshPending: false, jobsByAccount: queueJobs, profilesByAccount: new Map(), accountIssues: new Map(), cooldowns: new Map(),
    ACTIVE: new Set(["waiting"]), $: () => ({}), renderAccounts() {}, renderRequests() {}, notice() {},
    async refreshProfile() {},
    async api(account, route) {
      assert.equal(route, "/api/requests", "Dashboard refresh reads saved status; the server handles upstream polling");
      return {requests: [waitingJob]};
    },
  });
  vm.runInContext(source.slice(start, end), queueView);
  await queueView.refresh();
  assert.equal(queueJobs.get("account")[0].id, "local-request");
  assert.equal(queueJobs.get("account")[0].queue_position, 2);
  console.log("Dashboard retains queued local requests without upstream task IDs");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
