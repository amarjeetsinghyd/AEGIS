module.exports = {
  apps: [
    {
      name: "aegis-bot",
      script: "AEGIS/aegis_main.py",
      cwd: "/home/opc/quant-bot",
      interpreter: "python3.12",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        TZ: "Asia/Kolkata",
        PYTHONPATH: "/home/opc/quant-bot",
        BOT_RUNTIME: "aegis",
        MAX_TRADES_PER_DAY: "3",
        AEGIS_CONSEC_LOSS_STOP: "2",
        AEGIS_DAILY_LOSS_CAP_RS: "2000.0",
        AEGIS_ENTRY_CUTOFF: "15:00:00",
        AEGIS_FORCE_FLATTEN: "15:08:00",
        ENABLE_LIVE_BROKERAGE_EXECUTION: "True",
        AEGIS_LIVE_TRADING: "TRUE"
      }
    },
    {
      name: "unified-dashboard",
      script: "unified_dashboard/dashboard.py",
      cwd: "/home/opc/quant-bot",
      interpreter: "python3.12",
      autorestart: true,
      watch: false,
      max_memory_restart: "400M",
      max_restarts: 10,
      restart_delay: 3000,
      env: {
        TZ: "Asia/Kolkata",
        PYTHONPATH: "/home/opc/quant-bot"
      }
    }
  ]
};
