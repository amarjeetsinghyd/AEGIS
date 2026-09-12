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
        BOT_RUNTIME: "aegis",
        AEGIS_ML_GATEKEEPER_MODE: "ACTIVE",
        AEGIS_QUALITY_STACK_MODE: "ACTIVE",
        MAX_TRADES_PER_DAY: "3",
        AEGIS_CONSEC_LOSS_STOP: "2",
        AEGIS_DAILY_LOSS_CAP_RS: "2000.0",
        AEGIS_ENTRY_CUTOFF: "15:00:00",
        AEGIS_FORCE_FLATTEN: "15:08:00"
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
    }
  ]
};
