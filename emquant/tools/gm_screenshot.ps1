# gm_screenshot.ps1 -- capture the Goldminer3 terminal display to a PNG.
# Zero-dependency observation path (no CUA): System.Drawing CopyFromScreen.
# Usage: powershell -ExecutionPolicy Bypass -File gm_screenshot.ps1 <out.png> [display]
#   display: 1 = main (origin 0,0), 2 = left secondary (origin -2560,0). Default 2.
param([string]$OutPath = "$env:TEMP\gm_shot.png", [int]$Display = 2)
Add-Type -AssemblyName System.Drawing
$x = 0; if ($Display -eq 2) { $x = -2560 }
$w = 2560; $h = 1440
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, 0, 0, 0, $bmp.Size)
$g.Dispose()
$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output $OutPath
