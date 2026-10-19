// Validate the launch contract without starting the user's PM2 daemon.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const configPath = path.join(__dirname, "../ecosystem.config.js");
assert.ok(fs.existsSync(configPath), "The PM2 launch configuration must exist");
function config(port) {
  delete require.cache[require.resolve(configPath)];
  if (port === undefined) delete process.env.PLENX_PORT;
  else process.env.PLENX_PORT = port;
  process.env.PLENX_UV_BIN = "/home/deploy/.local/bin/uv";
  return require(configPath).apps[0];
}

const app = config();
assert.equal(app.name, "plenx-service");
assert.equal(app.script, "/home/deploy/.local/bin/uv");
assert.equal(app.interpreter, "none");
assert.equal(app.cwd, path.join(__dirname, ".."));
assert.equal(app.instances, 1);
assert.deepEqual(app.args, ["run", "--frozen", "--no-sync", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]);
assert.ok(!app.args.includes("--reload"));
const custom = config("9000");
assert.equal(custom.args[custom.args.indexOf("--port") + 1], "9000");
for (const port of ["0", "65536", "80; whoami", "-1", "0010"]) {
  assert.throws(() => config(port), /port/i);
}
console.log("PM2 launch configuration passed");
