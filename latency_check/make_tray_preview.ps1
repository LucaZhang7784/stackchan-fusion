Add-Type -AssemblyName System.Drawing

function Draw-RobotIcon($path, $state, $gw, $mcp, $robot, $scale) {
    $bmp = New-Object System.Drawing.Bitmap (32 * $scale), (32 * $scale)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.ScaleTransform($scale, $scale)
    switch ($state) {
        'ok'   { $c1 = [System.Drawing.Color]::FromArgb(46, 204, 113); $c2 = [System.Drawing.Color]::FromArgb(22, 160, 133); break }
        'warn' { $c1 = [System.Drawing.Color]::FromArgb(243, 156, 18); $c2 = [System.Drawing.Color]::FromArgb(211, 84, 0);  break }
        default{ $c1 = [System.Drawing.Color]::FromArgb(231, 76, 60);  $c2 = [System.Drawing.Color]::FromArgb(192, 57, 43); break }
    }
    $bgRect = New-Object System.Drawing.Rectangle 3, 8, 26, 21
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $bgRect, $c1, $c2, 45
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = 8
    $path.AddArc(3, 8, $d, $d, 180, 90)
    $path.AddArc(29 - $d, 8, $d, $d, 270, 90)
    $path.AddArc(29 - $d, 29 - $d, $d, $d, 0, 90)
    $path.AddArc(3, 29 - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    $g.FillPath($brush, $path)
    $dotBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
    $dotOff   = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(120, 255, 255, 255))
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(230, 255, 255, 255)), 2
    $g.DrawLine($pen, 16, 8, 16, 3)
    $antBrush = if ($robot) { $dotBrush } else { $dotOff }
    $g.FillEllipse($antBrush, 13, 1, 6, 6)
    $eyeL = if ($gw)  { $dotBrush } else { $dotOff }
    $g.FillEllipse($eyeL, 7, 14, 7, 7)
    $eyeR = if ($mcp) { $dotBrush } else { $dotOff }
    $g.FillEllipse($eyeR, 18, 14, 7, 7)
    $g.FillRectangle($dotBrush, 11, 24, 10, 2)
    $g.Dispose()
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}

$out = "${STACKCHAN_ROOT}\fusion.firmware.0731\latency_check"
Draw-RobotIcon "$out\tray_robot_ok.png"   'ok'   $true  $true  $true  8
Draw-RobotIcon "$out\tray_robot_warn.png" 'warn' $true  $false $true  8
Draw-RobotIcon "$out\tray_robot_bad.png"  'bad'  $false $false $false 8
Write-Output "PREVIEW SAVED"
