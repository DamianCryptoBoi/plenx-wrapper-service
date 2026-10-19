const port = process.env.PLENX_PORT || "8000";
if (!/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535) {
  throw new Error("PLENX_PORT must be a port number from 1 to 65535");
}

module.exports = {
  apps: [{
    name: "plenx-service",
    cwd: __dirname,
    script: process.env.PLENX_UV_BIN || "uv",
    interpreter: "none",
    args: ["run", "--frozen", "--no-sync", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", port, "--workers", "1"],
    exec_mode: "fork",
    instances: 1,
    watch: false,
    autorestart: true,
    restart_delay: 3000,
    kill_timeout: 10000,
    time: true,
    env: {PYTHONUNBUFFERED: "1"},
  }],
};
