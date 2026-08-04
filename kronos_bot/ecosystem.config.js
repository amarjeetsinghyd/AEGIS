module.exports = {
  apps: [
    {
      name: "astra-bot",
      script: "astra_bot/main.py",
      interpreter: "/home/opc/astra_bot/venv/bin/python3",
      cwd: "/home/opc/astra_bot",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      kill_timeout: 15000,
      env: {
        PYTHONPATH: "/home/opc/astra_bot"
      }
    },
    {
      name: "astra-dashboard",
      script: "astra_bot/dashboard.py",
      interpreter: "/home/opc/astra_bot/venv/bin/python3",
      cwd: "/home/opc/astra_bot",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      env: {
        PYTHONPATH: "/home/opc/astra_bot"
      }
    },
    {
      name: "kronos-bot",
      script: "kronos_bot/main.py",
      interpreter: "/home/opc/kronos_bot/venv/bin/python3",
      cwd: "/home/opc/kronos_bot",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      kill_timeout: 15000,
      env: {
        PYTHONPATH: "/home/opc/kronos_bot"
      }
    },
    {
      name: "kronos-dashboard",
      script: "kronos_bot/dashboard.py",
      interpreter: "/home/opc/kronos_bot/venv/bin/python3",
      cwd: "/home/opc/kronos_bot",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      env: {
        PYTHONPATH: "/home/opc/kronos_bot"
      }
    }
  ]
};
