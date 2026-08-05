module.exports = {
  apps: [
    {
      name: "shoonya-data-hub",
      script: "shoonya_data_hub/main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "500M",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "hub"
      }
    },
    {
      name: "astra-bot",
      script: "astra_bot/main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "astra"
      }
    },
    {
      name: "kronos-bot",
      script: "kronos_bot/main.py",
      cwd: "/home/opc",
      interpreter: "python3",
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONPATH: "/home/opc",
        BOT_RUNTIME: "kronos"
      }
    },
    {
      name: "stock-order-flow",
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
    }
  ]
};
