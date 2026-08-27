# gm_sendkeys.ps1 -- activate the Goldminer3 terminal window and send keystrokes.
# Zero-dependency input path (no CUA): WScript.Shell AppActivate + SendKeys.
# Usage: powershell -ExecutionPolicy Bypass -File gm_sendkeys.ps1 '^+I' [waitMs]
#   keys: SendKeys syntax (^=Ctrl +=Shift !=Alt, e.g. '^+I' = Ctrl+Shift+I)
# NOTE: SendKeys types into whatever has focus once activated -- keep the
# terminal frontmost during the wait window; chars needing IME are unreliable.
param([string]$Keys = '^+I', [int]$WaitMs = 1200)
$ws = New-Object -ComObject WScript.Shell
$ok = $false
foreach ($proc in (Get-Process emgm3 -ErrorAction SilentlyContinue)) {
    if ($ws.AppActivate($proc.Id)) { $ok = $true; break }
}
if (-not $ok) { $ok = $ws.AppActivate('Eastmoney Juejin Quant Terminal') }
if (-not $ok) { Write-Output 'ACTIVATE_FAIL'; exit 1 }
Start-Sleep -Milliseconds 400
$ws.SendKeys($Keys)
Start-Sleep -Milliseconds $WaitMs
Write-Output "SENT: $Keys"
