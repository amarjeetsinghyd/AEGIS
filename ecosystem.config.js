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
      name: "l2-archiver",
      script: "stock_order_flow/l2_archiver.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "500M",
      max_restarts: 10,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "stock_order_flow"
      }
    },
    {
      name: "argus",
      script: "stock_order_flow/main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "stock_order_flow"
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
      script: "stock_order_flow/eod_calibrator.py",
      args: "--cron",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "stock_order_flow"
      }
    }
  ]
};
