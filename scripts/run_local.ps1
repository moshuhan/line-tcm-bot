# 本地快速測試 LINE Bot
# 用法：從專案根目錄執行 .\scripts\run_local.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  LINE TCM Bot - 本地測試" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Flask 啟動於 http://0.0.0.0:5000" -ForegroundColor Green
Write-Host ""
Write-Host "下一步：" -ForegroundColor Yellow
Write-Host "  1. 另開終端執行: ngrok http 5000" -ForegroundColor White
Write-Host "  2. 複製 ngrok 提供的 https 網址（如 https://xxxx.ngrok-free.app）" -ForegroundColor White
Write-Host "  3. LINE Developers Console -> Messaging API -> Webhook URL 設為：" -ForegroundColor White
Write-Host "     https://你的ngrok網址/callback" -ForegroundColor Magenta
Write-Host "  4. 在 LINE 傳訊息給 Bot 即可即時測試" -ForegroundColor White
Write-Host ""
Write-Host "測試完成後記得把 Webhook 改回 Vercel 網址！" -ForegroundColor Yellow
Write-Host ""

python -m api.index
