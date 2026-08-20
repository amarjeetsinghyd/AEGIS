module.exports = {
  apps: [
    {
      name: "aegis-bot",
      script: "AEGIS/aegis_main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "aegis"
      }
    },
    {
      name: "argus",
      script: "ARGUS/main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "ARGUS"
      }
    },
    {
      name: "unified-dashboard",
      script: "unified_dashboard/dashboard.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "400M",
      max_restarts: 10,
      restart_delay: 3000,
      env: {
        PYTHONPATH: "/home/opc"
      }
    },
    {
      name: "eod-calibrator",
      script: "ARGUS/eod_calibrator.py",
      args: "--cron",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "ARGUS"
      }
    },
    {
      name: "stratos-bot",
      script: "STRATOS/main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "stratos"
      }
    }
  ]
};
