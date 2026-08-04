#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# VPS Audit & Deployment Script
# ---------------------------------------------------------------------------
# This script connects to the remote Oracle Cloud VPS, optionally syncs the
# repository (if a git checkout exists), installs Python dependencies for both
# bots, restarts all PM2‑managed processes, and gathers a concise runtime audit
# (PM2 status, recent logs, Python version, top pip packages, open ports).
#
# Prerequisites:
#   * SSH key located at "shoonya_data_hub/Keys/ssh-key-2026-07-11.key"
#   * The user "opc" has password‑less sudo/permissions to run the commands.
#   * The remote repository is either at "~/Kronos_and_Astra" (preferred) or the
#     bots live directly under the home directory (~/astra_bot, ~/kronos_bot).
# ---------------------------------------------------------------------------

set -euo pipefail

# Configuration – adjust if your environment changes
# The SSH private key is stored in the folder "Oracle SSH keys" at the workspace root.
# Note the space in the folder name – it is quoted inside the string so Bash can handle it.
SSH_KEY="Oracle SSH keys/ssh-key-2026-07-11.key"
SERVER="opc@137.23.41.38"
REMOTE_DIR="~/Kronos_and_Astra"

# Helper to run a remote command via SSH
ssh_remote() {
    local cmd="$1"
    ssh -i "$SSH_KEY" "$SERVER" "$cmd"
}

# Ensure the private key has strict permissions before SSH attempts to use it.
# This mirrors the Unix `chmod 600` requirement that SSH enforces.
chmod 600 "$SSH_KEY"

echo "--- Connecting to VPS $SERVER ---"

# ---------------------------------------------------------------------------
# 1. Determine the correct remote project directory
# ---------------------------------------------------------------------------
echo "Checking if $REMOTE_DIR exists..."
if ssh_remote "[[ -d $REMOTE_DIR ]]"; then
    PROJECT_PATH="$REMOTE_DIR"
    echo "Found combined repo at $PROJECT_PATH"
else
    # Fallback: use home directory where the two bots are present
    PROJECT_PATH="~"
    echo "Combined repo not found – using home directory as project root"
fi

# ---------------------------------------------------------------------------
# 2. Optional Git sync (only if a .git folder is present)
# ---------------------------------------------------------------------------
echo "--- Git sync (if applicable) ---"
ssh_remote "if [[ -d $PROJECT_PATH/.git ]]; then cd $PROJECT_PATH && git fetch && git reset --hard origin/main; else echo 'No .git repository – skipping sync'; fi"

# ---------------------------------------------------------------------------
# 3. Install Python dependencies for both bots
# ---------------------------------------------------------------------------
echo "--- Installing Python dependencies ---"
ssh_remote "cd $PROJECT_PATH && \
    if [[ -f astra_bot/requirements.txt ]]; then pip install -r astra_bot/requirements.txt; else echo 'astra_bot/requirements.txt not found'; fi && \
    if [[ -f kronos_bot/requirements.txt ]]; then pip install -r kronos_bot/requirements.txt; else echo 'kronos_bot/requirements.txt not found'; fi"

# ---------------------------------------------------------------------------
# 4. Restart PM2 processes and collect basic status
# ---------------------------------------------------------------------------
echo "--- Restarting PM2 processes and gathering status ---"
ssh_remote "cd $PROJECT_PATH && \
    pm2 restart all && \
    echo '--- PM2 STATUS ---' && pm2 status && \
    echo '--- PM2 LOGS (last 20 lines) ---' && pm2 logs --lines 20"

# ---------------------------------------------------------------------------
# 5. Runtime audit information
# ---------------------------------------------------------------------------
echo "--- Runtime audit information ---"
ssh_remote "echo '--- PYTHON VERSION ---' && python --version && \
    echo '--- TOP 10 PIP PACKAGES ---' && pip list | head -n 10 && \
    echo '--- OPEN NETWORK PORTS ---' && netstat -tulnp || true"

echo "--- Audit completed ---"
