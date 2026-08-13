$ErrorActionPreference = "Stop"

$sshKey = "C:\Users\Amarjeet Singh\quant-bot\stock_order_flow\Oracle SSH keys\ssh-key-2026-07-11.key"
$userHost = "opc@137.23.41.38"
$remoteDir = "/home/opc"

ssh -o StrictHostKeyChecking=no -i "$sshKey" $userHost "mkdir -p ${remoteDir}/astra_bot"
scp -o StrictHostKeyChecking=no -i "$sshKey" -r "C:\Users\Amarjeet Singh\quant-bot\astra_bot\*" "${userHost}:${remoteDir}/astra_bot/"

Write-Host "Syncing common/ipc directory..."
scp -o StrictHostKeyChecking=no -i "$sshKey" -r "C:\Users\Amarjeet Singh\quant-bot\common\ipc" "${userHost}:${remoteDir}/common/"

Write-Host "Syncing common/execution directory..."
scp -o StrictHostKeyChecking=no -i "$sshKey" -r "C:\Users\Amarjeet Singh\quant-bot\common\execution" "${userHost}:${remoteDir}/common/"

Write-Host "Syncing unified_dashboard..."
scp -o StrictHostKeyChecking=no -i "$sshKey" "C:\Users\Amarjeet Singh\quant-bot\unified_dashboard\dashboard.py" "${userHost}:${remoteDir}/unified_dashboard/"

Write-Host "Syncing stock_order_flow..."
scp -o StrictHostKeyChecking=no -i "$sshKey" -r "C:\Users\Amarjeet Singh\quant-bot\stock_order_flow\*" "${userHost}:${remoteDir}/stock_order_flow/"

Write-Host "Restarting PM2 services (astra-bot, unified-dashboard, stock-order-flow)..."
ssh -o StrictHostKeyChecking=no -i "$sshKey" $userHost "cd $remoteDir && pm2 restart astra-bot unified-dashboard stock-order-flow"

Write-Host "Deployment Complete!"
