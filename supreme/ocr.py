"""Распознавание текста на скриншотах встроенным в Windows 10/11 распознаванием
(Windows.Media.Ocr). Ничего устанавливать не нужно: используется PowerShell.

Скриншот сначала подготавливается (System.Drawing): увеличивается, чтобы мелкий
текст игры был ~40 px, и читается в нескольких вариантах — белый текст как
чёрный на белом (названия на цветных полосах и значки «x40» в MM2 белые),
инверсия яркости, исходные цвета. Результат — слова с их местом на картинке;
supreme.tileocr собирает из них плитки инвентаря (название + количество).
"""

import os
import subprocess
import tempfile

from . import tileocr

TIMEOUT = 150
PASSES = "white,grayinv,whitehi,color"


class OcrError(Exception):
    pass


def available():
    return os.name == "nt"


# Скрипт PowerShell 5.1 (только System.Drawing и Windows.Media.Ocr). Печатает одну строку
# "@@MM2OCR@@{json}" — JSON только из ASCII, координаты в пикселях исходной картинки.
_TILE_SCRIPT = r"""param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Passes = 'white,grayinv,whitehi,color',
    [double]$Scale = 0,
    [double]$TargetHeight = 40,
    [int]$MaxSide = 4000,
    [int]$Overlap = 320,
    [string]$Lang = 'en-US',
    [string]$Regions = '',
    [string]$DumpDir = ''
)
# MM2 inventory OCR: Windows.Media.Ocr + System.Drawing preprocessing, no installs.
# Prints one line: @@MM2OCR@@{json}. The JSON is pure ASCII (non-ASCII is \uXXXX-escaped),
# coordinates are integers in the ORIGINAL image pixels.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$script:Marker = '@@MM2OCR@@'

# ---------------------------------------------------------------- pure helpers
function ConvertTo-JsonString([string]$s) {
    if ($null -eq $s) { return 'null' }
    $sb = New-Object System.Text.StringBuilder ($s.Length + 8)
    [void]$sb.Append('"')
    foreach ($ch in $s.ToCharArray()) {
        $c = [int]$ch
        if ($c -eq 34) { [void]$sb.Append('\"') }
        elseif ($c -eq 92) { [void]$sb.Append('\\') }
        elseif ($c -lt 32 -or $c -gt 126) { [void]$sb.Append(('\u{0:x4}' -f $c)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Format-Num([double]$v) {
    return $v.ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
}

# Start offsets of overlapping windows of length <= $maxLen that cover [0, $len).
function Get-Spans([int]$len, [int]$maxLen, [int]$overlap) {
    $spans = New-Object System.Collections.Generic.List[int[]]
    if ($len -le $maxLen) { $spans.Add([int[]]@(0, $len)); return , $spans }
    if ($overlap -gt [int]($maxLen / 2)) { $overlap = [int]($maxLen / 2) }
    $n = [int][Math]::Ceiling(($len - $overlap) / [double]($maxLen - $overlap))
    if ($n -lt 2) { $n = 2 }
    $step = ($len - $maxLen) / [double]($n - 1)
    for ($i = 0; $i -lt $n; $i++) {
        $start = [int][Math]::Round($i * $step)
        $spans.Add([int[]]@($start, $maxLen))
    }
    return , $spans
}

# Source rectangles (x, y, w, h) whose scaled size fits $maxSide.
function Get-TilePlan([int]$width, [int]$height, [double]$scale, [int]$maxSide, [int]$overlap) {
    $srcMax = [int][Math]::Floor($maxSide / $scale)
    $plan = New-Object System.Collections.Generic.List[int[]]
    foreach ($ys in (Get-Spans $height $srcMax $overlap)) {
        foreach ($xs in (Get-Spans $width $srcMax $overlap)) {
            $plan.Add([int[]]@($xs[0], $ys[0], $xs[1], $ys[1]))
        }
    }
    return , $plan
}

# Median height (original px) of words that look like text (2+ letters/digits).
function Get-MedianHeight($words) {
    $hs = New-Object System.Collections.Generic.List[double]
    foreach ($word in $words) {
        if ($word.Text -match '[A-Za-z0-9]{2,}') { $hs.Add([double]$word.H) }
    }
    if ($hs.Count -eq 0) { return 0.0 }
    $arr = $hs.ToArray()
    [Array]::Sort($arr)
    return $arr[[int][Math]::Floor(($arr.Length - 1) / 2)]
}

# Scale that makes the median word ~$target px tall, clamped.
function Get-AutoScale([double]$medianHeight, [double]$target, [double]$fallback) {
    if ($medianHeight -le 0) { return $fallback }
    $s = $target / $medianHeight
    if ($s -lt 0.5) { $s = 0.5 }
    if ($s -gt 5.0) { $s = 5.0 }
    return $s
}

# Word box (engine coords inside a scaled crop) -> original image box + 'cut' flag.
function Convert-Box($rect, [int[]]$crop, [double]$sx, [double]$sy, [int]$scaledW, [int]$scaledH, [int]$imgW, [int]$imgH) {
    $x0 = [int][Math]::Floor($crop[0] + $rect.X / $sx)
    $y0 = [int][Math]::Floor($crop[1] + $rect.Y / $sy)
    $x1 = [int][Math]::Ceiling($crop[0] + ($rect.X + $rect.Width) / $sx)
    $y1 = [int][Math]::Ceiling($crop[1] + ($rect.Y + $rect.Height) / $sy)
    if ($x0 -lt 0) { $x0 = 0 }
    if ($y0 -lt 0) { $y0 = 0 }
    if ($x1 -gt $imgW) { $x1 = $imgW }
    if ($y1 -gt $imgH) { $y1 = $imgH }
    $edge = 3
    $cut = (($crop[0] -gt 0 -and $rect.X -le $edge) -or
        ($crop[1] -gt 0 -and $rect.Y -le $edge) -or
        (($crop[0] + $crop[2]) -lt $imgW -and ($rect.X + $rect.Width) -ge ($scaledW - $edge)) -or
        (($crop[1] + $crop[3]) -lt $imgH -and ($rect.Y + $rect.Height) -ge ($scaledH - $edge)))
    return [int[]]@($x0, $y0, ($x1 - $x0), ($y1 - $y0), [int]$cut)
}

function ConvertTo-WordJson($word) {
    return ('{{"text":{0},"x":{1},"y":{2},"w":{3},"h":{4},"pass":{5},"line":{6},"cut":{7},"bg":[{8},{9},{10}]}}' -f
        (ConvertTo-JsonString $word.Text), $word.X, $word.Y, $word.W, $word.H, (ConvertTo-JsonString $word.Pass),
        (ConvertTo-JsonString $word.Line), $word.Cut, $word.Bg[0], $word.Bg[1], $word.Bg[2])
}

function Write-Result([string]$json) {
    [Console]::Out.Write($script:Marker + $json + "`n")
    [Console]::Out.Flush()
}

if ($env:MM2OCR_DEFINE_ONLY -eq '1') { return }   # used by tests: load the helpers only

# ---------------------------------------------------------------- Windows APIs
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

    $script:AsTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Await($operation, [Type]$type) {
        $task = $script:AsTask.MakeGenericMethod($type).Invoke($null, @($operation))
        $task.Wait(-1) | Out-Null
        return $task.Result
    }

    # English first (MM2 names are English), then the user's languages.
    $script:Engine = $null
    foreach ($tag in @($Lang, 'en-US', 'en-GB')) {
        try {
            $language = [Windows.Globalization.Language]::new($tag)
            if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($language)) {
                $script:Engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
            }
        } catch { }
        if ($null -ne $script:Engine) { break }
    }
    if ($null -eq $script:Engine) { $script:Engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $script:Engine) {
        Write-Result '{"ok":false,"code":"NO_OCR_ENGINE","error":"no OCR language installed"}'
        exit 3
    }
    $maxDim = [int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    $limit = [Math]::Min($maxDim, $MaxSide)
    $script:WorkDir = Split-Path -Parent ([IO.Path]::GetFullPath($Path))
    $script:UseTempFile = $false
    $script:TempN = 0

    # ---- load the screenshot into a 32bpp bitmap (stream copy: no file lock)
    $bytes = [IO.File]::ReadAllBytes($Path)
    $inStream = New-Object IO.MemoryStream (, $bytes)
    $img = [System.Drawing.Image]::FromStream($inStream)
    $ImgW = $img.Width; $ImgH = $img.Height
    $src = New-Object System.Drawing.Bitmap ($ImgW, $ImgH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($src)
    $g.Clear([System.Drawing.Color]::Black)
    $g.DrawImage($img, 0, 0, $ImgW, $ImgH)
    $g.Dispose(); $img.Dispose(); $inStream.Dispose()

    function New-ColorMatrix([double[]]$m) {
        $rows = New-Object 'float[][]' 5
        for ($i = 0; $i -lt 5; $i++) {
            $row = New-Object 'float[]' 5
            for ($j = 0; $j -lt 5; $j++) { $row[$j] = [float]$m[$i * 5 + $j] }
            $rows[$i] = $row
        }
        return [System.Drawing.Imaging.ColorMatrix]::new($rows)
    }

    function Invoke-Attributes([System.Drawing.Bitmap]$bmp, [System.Drawing.Imaging.ImageAttributes]$attr) {
        $out = New-Object System.Drawing.Bitmap ($bmp.Width, $bmp.Height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $gr = [System.Drawing.Graphics]::FromImage($out)
        try {
            $gr.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
            $gr.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
            $gr.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
            $rect = New-Object System.Drawing.Rectangle (0, 0, $bmp.Width, $bmp.Height)
            $gr.DrawImage($bmp, $rect, 0, 0, $bmp.Width, $bmp.Height, [System.Drawing.GraphicsUnit]::Pixel, $attr)
        } finally { $gr.Dispose() }
        return $out
    }

    function New-ScaledCrop([System.Drawing.Bitmap]$bmp, [int[]]$crop, [double]$s) {
        $w = [Math]::Max(1, [int][Math]::Round($crop[2] * $s))
        $h = [Math]::Max(1, [int][Math]::Round($crop[3] * $s))
        $out = New-Object System.Drawing.Bitmap ($w, $h, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $gr = [System.Drawing.Graphics]::FromImage($out)
        $attr = New-Object System.Drawing.Imaging.ImageAttributes
        try {
            $attr.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)   # no dark fringe at crop edges
            $gr.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
            $gr.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
            $gr.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $gr.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $rect = New-Object System.Drawing.Rectangle (0, 0, $w, $h)
            $gr.DrawImage($bmp, $rect, $crop[0], $crop[1], $crop[2], $crop[3], [System.Drawing.GraphicsUnit]::Pixel, $attr)
        } finally { $gr.Dispose(); $attr.Dispose() }
        return $out
    }

    # White-ish pixels (every channel > t) -> black ink, everything else -> white paper.
    # Step 1: per-channel threshold (GDI+ SetThreshold), step 2: out = 3 - R - G - B (clamped).
    function Convert-WhiteMask([System.Drawing.Bitmap]$bmp, [double]$t) {
        $a1 = New-Object System.Drawing.Imaging.ImageAttributes
        $a2 = New-Object System.Drawing.Imaging.ImageAttributes
        try {
            $a1.SetThreshold([float]$t)
            $tmp = Invoke-Attributes $bmp $a1
            $a2.SetColorMatrix((New-ColorMatrix @(-1, -1, -1, 0, 0, -1, -1, -1, 0, 0, -1, -1, -1, 0, 0, 0, 0, 0, 1, 0, 3, 3, 3, 0, 1)))
            $out = Invoke-Attributes $tmp $a2
            $tmp.Dispose()
            return $out
        } finally { $a1.Dispose(); $a2.Dispose() }
    }

    # Inverted luma with contrast k: out = k * (1 - Y). White text -> black, dark background -> white.
    function Convert-GrayInv([System.Drawing.Bitmap]$bmp, [double]$k) {
        $r = -$k * 0.299; $gg = -$k * 0.587; $b = -$k * 0.114
        $attr = New-Object System.Drawing.Imaging.ImageAttributes
        try {
            $attr.SetColorMatrix((New-ColorMatrix @($r, $r, $r, 0, 0, $gg, $gg, $gg, 0, 0, $b, $b, $b, 0, 0, 0, 0, 0, 1, 0, $k, $k, $k, 0, 1)))
            return Invoke-Attributes $bmp $attr
        } finally { $attr.Dispose() }
    }

    function New-PassBitmap([System.Drawing.Bitmap]$scaled, [string]$kind) {
        switch ($kind) {
            'white' { return Convert-WhiteMask $scaled 0.72 }
            'whitehi' { return Convert-WhiteMask $scaled 0.90 }
            'grayinv' { return Convert-GrayInv $scaled 1.5 }
            'color' { return $scaled.Clone() }
            default { throw "unknown pass $kind" }
        }
    }

    function ConvertTo-SoftwareBitmap([System.Drawing.Bitmap]$bmp) {
        $rgb = $bmp.Clone((New-Object System.Drawing.Rectangle (0, 0, $bmp.Width, $bmp.Height)), [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
        $ms = New-Object System.IO.MemoryStream
        try {
            $rgb.Save($ms, [System.Drawing.Imaging.ImageFormat]::Bmp)
            if (-not $script:UseTempFile) {
                try {
                    $ms.Position = 0
                    $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
                    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
                    return Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
                } catch { $script:UseTempFile = $true }
            }
            # Fallback: the StorageFile path (same as the old, known-working script).
            $tmp = Join-Path $script:WorkDir ('mm2ocr-{0}-{1}.bmp' -f $PID, $script:TempN)
            $script:TempN++
            [IO.File]::WriteAllBytes($tmp, $ms.ToArray())
            try {
                $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
                $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
                try {
                    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                    return Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
                } finally { $stream.Dispose() }
            } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        } finally { $rgb.Dispose(); $ms.Dispose() }
    }

    # Colour around the word (left, right, above; the most saturated sample): banner colour = rarity.
    function Get-Background([int]$x, [int]$y, [int]$w, [int]$h) {
        $cy = $y + [int]($h / 2)
        $points = @(
            @(($x - [int]($h * 0.35) - 1), $cy),
            @(($x + $w + [int]($h * 0.35)), $cy),
            @(($x + [int]($w / 2)), ($y - [Math]::Max(2, [int]($h * 0.2)))))
        $best = @(0, 0, 0); $bestSat = -1
        foreach ($pt in $points) {
            $px = [Math]::Min($ImgW - 1, [Math]::Max(0, $pt[0]))
            $py = [Math]::Min($ImgH - 1, [Math]::Max(0, $pt[1]))
            $c = $src.GetPixel($px, $py)
            $sat = [Math]::Max($c.R, [Math]::Max($c.G, $c.B)) - [Math]::Min($c.R, [Math]::Min($c.G, $c.B))
            if ($sat -gt $bestSat) { $bestSat = $sat; $best = @([int]$c.R, [int]$c.G, [int]$c.B) }
        }
        return $best
    }

    # ---- Regions mode: re-read small areas (quantity badges "x3" that the full pass missed).
    # Windows OCR skips a lone short word such as "x3" in an empty picture. So in every area
    # the bright neutral pixels (the badge text is white) are found, cropped tightly, enlarged
    # to ~44 px glyphs and drawn four times side by side like a line of words ("x3 x3 x3 x3");
    # areas are stacked on one sheet with blank rows between them. Words carry the index of
    # their area; "areas" tells where bright text was found (null = nothing like a badge).
    function Get-BadgeInk([int[]]$a) {
        if ($a[2] -le 0 -or $a[3] -le 0) { return $null }
        $rect = New-Object System.Drawing.Rectangle ($a[0], $a[1], $a[2], $a[3])
        $data = $src.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        try {
            $stride = $data.Stride
            $bytes = New-Object byte[] ($stride * $a[3])
            [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $bytes.Length)
        } finally { $src.UnlockBits($data) }
        $minX = $a[2]; $minY = $a[3]; $maxX = -1; $maxY = -1; $count = 0
        for ($y = 0; $y -lt $a[3]; $y++) {
            $i = $y * $stride
            for ($x = 0; $x -lt $a[2]; $x++) {
                $b = $bytes[$i]; $g = $bytes[$i + 1]; $r = $bytes[$i + 2]
                $i += 4
                if ($r -lt 170 -or $g -lt 170 -or $b -lt 170) { continue }
                $hi = $r; if ($g -gt $hi) { $hi = $g }; if ($b -gt $hi) { $hi = $b }
                $lo = $r; if ($g -lt $lo) { $lo = $g }; if ($b -lt $lo) { $lo = $b }
                if (($hi - $lo) -gt 40) { continue }
                $count++
                if ($x -lt $minX) { $minX = $x }; if ($x -gt $maxX) { $maxX = $x }
                if ($y -lt $minY) { $minY = $y }; if ($y -gt $maxY) { $maxY = $y }
            }
        }
        if ($maxX -lt 0 -or $count -lt 4) { return $null }
        return , [int[]]@(($a[0] + $minX), ($a[1] + $minY), ($maxX - $minX + 1), ($maxY - $minY + 1), $count)
    }

    if ($Regions) {
        $areas = New-Object System.Collections.Generic.List[int[]]
        foreach ($part in $Regions.Split(';')) {
            $v = @($part.Split(',') | ForEach-Object { [int]$_ })
            if ($v.Count -ne 4) { continue }
            $x0 = [Math]::Max(0, $v[0]); $y0 = [Math]::Max(0, $v[1])
            $x1 = [Math]::Min($ImgW, $v[0] + $v[2]); $y1 = [Math]::Min($ImgH, $v[1] + $v[3])
            if (($x1 - $x0) -ge 4 -and ($y1 - $y0) -ge 4) { $areas.Add([int[]]@($x0, $y0, ($x1 - $x0), ($y1 - $y0), $areas.Count)) }
            else { $areas.Add([int[]]@(0, 0, 0, 0, $areas.Count)) }
        }
        $margin = 40
        $script:Debug = New-Object System.Collections.Generic.List[string]
        $inks = New-Object System.Collections.Generic.List[object]
        foreach ($a in $areas) { $inks.Add((Get-BadgeInk $a)) }
        $todo = New-Object System.Collections.Generic.List[int]
        for ($ai = 0; $ai -lt $areas.Count; $ai++) { if ($null -ne $inks[$ai]) { $todo.Add($ai) } }

        # One sheet: a row per area (indexes $idx), each row = optional prefix word + $copies
        # copies of the piece. mode 'tight' = bright text cropped tightly and scaled to $glyphH;
        # 'area' = the whole area scaled by $glyphH / 16. Returns @(bitmap, rows) or $null.
        function New-RegionSheet($idx, [string]$kind, [string]$mode, [double]$glyphH, [string]$prefix, [int]$copies, [double]$spaceK) {
            $pieces = New-Object System.Collections.Generic.List[object]
            foreach ($ai in $idx) {
                if ($mode -eq 'tight') {
                    $ink = $inks[$ai]
                    $s = [Math]::Min(8.0, [Math]::Max(1.0, $glyphH / [double][Math]::Max(1, $ink[3])))
                    $pad = [Math]::Max(2, [int][Math]::Round(0.35 * $ink[3]))
                    $cx0 = [Math]::Max(0, $ink[0] - $pad); $cy0 = [Math]::Max(0, $ink[1] - $pad)
                    $cx1 = [Math]::Min($ImgW, $ink[0] + $ink[2] + $pad); $cy1 = [Math]::Min($ImgH, $ink[1] + $ink[3] + $pad)
                    $crop = @($cx0, $cy0, ($cx1 - $cx0), ($cy1 - $cy0))
                } else {
                    $a = $areas[$ai]; $s = $glyphH / 16.0; $crop = @($a[0], $a[1], $a[2], $a[3])
                }
                $scaled = New-ScaledCrop $src $crop $s
                try { $pieces.Add((New-PassBitmap $scaled $kind)) } finally { $scaled.Dispose() }
            }
            if ($pieces.Count -eq 0) { return $null }
            $space = [int]($spaceK * $glyphH)
            $font = $null; $prefixW = 0
            if ($prefix) {
                $font = New-Object System.Drawing.Font('Arial', [float]($glyphH / 0.72), [System.Drawing.GraphicsUnit]::Pixel)
                $probe = New-Object System.Drawing.Bitmap (8, 8)
                $gp = [System.Drawing.Graphics]::FromImage($probe)
                try { $prefixW = [int][Math]::Ceiling($gp.MeasureString($prefix, $font).Width) + [int](0.6 * $glyphH) } finally { $gp.Dispose(); $probe.Dispose() }
            }
            $sheetW = 2 * $margin; $top = $margin
            $rows = @()
            foreach ($piece in $pieces) {
                $sheetW = [Math]::Max($sheetW, 2 * $margin + $prefixW + $copies * $piece.Width + ($copies - 1) * $space)
                $rows += , @($top, ($top + $piece.Height))
                $top += $piece.Height + [Math]::Max($piece.Height, [int](1.6 * $glyphH))
            }
            $sheetW = [Math]::Min($sheetW, $limit)
            $sheetH = [Math]::Min($limit, $top + $margin)
            $sheet = New-Object System.Drawing.Bitmap ($sheetW, $sheetH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $gs = [System.Drawing.Graphics]::FromImage($sheet)
            try {
                $gs.Clear([System.Drawing.Color]::White)
                $gs.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
                for ($i = 0; $i -lt $pieces.Count; $i++) {
                    $piece = $pieces[$i]
                    $left = $margin
                    if ($font) {
                        $ty = $rows[$i][0] + ($piece.Height - $font.GetHeight()) / 2
                        $gs.DrawString($prefix, $font, [System.Drawing.Brushes]::Black, [float]$left, [float]$ty)
                        $left += $prefixW
                    }
                    for ($k = 0; $k -lt $copies; $k++) {
                        if ($left + $piece.Width -gt $sheetW) { break }
                        $gs.DrawImage($piece, $left, $rows[$i][0], $piece.Width, $piece.Height)
                        $left += $piece.Width + $space
                    }
                }
            } finally {
                $gs.Dispose()
                if ($font) { $font.Dispose() }
                foreach ($piece in $pieces) { $piece.Dispose() }
            }
            return , @($sheet, $rows)
        }

        function Read-Sheet([System.Drawing.Bitmap]$sheet) {
            $sb = ConvertTo-SoftwareBitmap $sheet
            try { return Await ($script:Engine.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult]) } finally { $sb.Dispose() }
        }

        if ($DumpDir -and $todo.Count -gt 0) {
            # experiment: which sheet layout does this OCR read at all?
            $variants = @(
                @('tight', 44, '', 4, 0.6), @('tight', 30, '', 4, 0.6), @('tight', 22, '', 4, 0.6),
                @('tight', 44, 'Qty', 4, 0.6), @('tight', 30, 'Qty', 2, 0.6), @('tight', 44, '', 4, 0.25),
                @('area', 32, '', 2, 0.0), @('area', 24, '', 1, 0.0), @('tight', 44, 'Amount', 1, 0.6))
            foreach ($kind in @('white', 'grayinv')) {
                foreach ($v in $variants) {
                    $name = '{0}/{1}{2}/{3}/x{4}/s{5}' -f $kind, $v[0], $v[1], $v[2], $v[3], $v[4]
                    try {
                        $built = New-RegionSheet $todo $kind $v[0] $v[1] $v[2] $v[3] $v[4]
                        try { $res = Read-Sheet $built[0] } finally { $built[0].Dispose() }
                        $script:Debug.Add(('{0}: {1}' -f $name, (($res.Lines | ForEach-Object { $_.Text }) -join ' | ')))
                    } catch { $script:Debug.Add(('{0} failed: {1}' -f $name, $_.Exception.Message)) }
                }
            }
        }

        $found = New-Object System.Collections.Generic.List[object]
        $infos = New-Object System.Collections.Generic.List[string]
        foreach ($kind in @('white', 'grayinv', 'whitehi')) {
            $t0 = [Diagnostics.Stopwatch]::StartNew()
            $err = ''
            $perSheet = [Math]::Max(1, [int][Math]::Floor(($limit - 2 * $margin) / 170))
            for ($start = 0; $start -lt $todo.Count; $start += $perSheet) {
                $idx = @($todo[$start..([Math]::Min($todo.Count, $start + $perSheet) - 1)])
                try {
                    $built = New-RegionSheet $idx $kind 'tight' 44 '' 4 0.6
                    $sheet = $built[0]; $rows = $built[1]
                    try {
                        if ($DumpDir) { try { $sheet.Save((Join-Path $DumpDir ('regions-{0}-{1}.png' -f $kind, $start)), [System.Drawing.Imaging.ImageFormat]::Png) } catch { } }
                        $res = Read-Sheet $sheet
                    } finally { $sheet.Dispose() }
                    $li = 0
                    foreach ($line in $res.Lines) {
                        foreach ($word in $line.Words) {
                            $cy = $word.BoundingRect.Y + $word.BoundingRect.Height / 2
                            $region = -1
                            for ($ri = 0; $ri -lt $rows.Count; $ri++) {
                                $gap = ($rows[$ri][1] - $rows[$ri][0]) / 2
                                if ($cy -ge ($rows[$ri][0] - $gap) -and $cy -lt ($rows[$ri][1] + $gap)) { $region = $idx[$ri] }
                            }
                            if ($region -lt 0) { continue }
                            $found.Add([pscustomobject]@{
                                    Text = $word.Text; X = [int]$word.BoundingRect.X; Y = [int]$word.BoundingRect.Y
                                    W = [int]$word.BoundingRect.Width; H = [int]$word.BoundingRect.Height
                                    Pass = $kind; Line = ('{0}:r{1}:{2}' -f $kind, $start, $li); Region = $region
                                })
                        }
                        $li++
                    }
                } catch { $err = $_.Exception.Message }
            }
            $infos.Add(('{{"name":{0},"ms":{1},"error":{2}}}' -f (ConvertTo-JsonString $kind), [int]$t0.Elapsed.TotalMilliseconds, (ConvertTo-JsonString $err)))
        }
        $src.Dispose()
        $sbOut = New-Object System.Text.StringBuilder
        [void]$sbOut.Append(('{{"ok":true,"mode":"regions","width":{0},"height":{1},"regions":{2},"passes":[' -f $ImgW, $ImgH, $areas.Count))
        [void]$sbOut.Append(($infos -join ','))
        [void]$sbOut.Append('],"debug":[')
        [void]$sbOut.Append((($script:Debug | ForEach-Object { ConvertTo-JsonString $_ }) -join ','))
        [void]$sbOut.Append('],"areas":[')
        for ($ai = 0; $ai -lt $areas.Count; $ai++) {
            if ($ai -gt 0) { [void]$sbOut.Append(',') }
            $ink = $inks[$ai]
            if ($null -eq $ink) { [void]$sbOut.Append(('{{"region":{0},"ink":null}}' -f $ai)) }
            else { [void]$sbOut.Append(('{{"region":{0},"ink":[{1},{2},{3},{4}],"pixels":{5}}}' -f $ai, $ink[0], $ink[1], $ink[2], $ink[3], $ink[4])) }
        }
        [void]$sbOut.Append('],"words":[')
        $first = $true
        foreach ($p in $found) {
            if (-not $first) { [void]$sbOut.Append(',') }
            [void]$sbOut.Append(('{{"text":{0},"region":{1},"pass":{2},"line":{3},"x":{4},"y":{5},"w":{6},"h":{7}}}' -f
                    (ConvertTo-JsonString $p.Text), $p.Region, (ConvertTo-JsonString $p.Pass), (ConvertTo-JsonString $p.Line),
                    $p.X, $p.Y, $p.W, $p.H))
            $first = $false
        }
        [void]$sbOut.Append(']}')
        Write-Result $sbOut.ToString()
        exit 0
    }

    $script:Words = New-Object System.Collections.Generic.List[object]
    $script:PassInfo = New-Object System.Collections.Generic.List[string]

    function Invoke-Passes([double]$s, [string[]]$kinds, [string]$suffix) {
        $plan = Get-TilePlan $ImgW $ImgH $s $limit $Overlap
        $found = New-Object System.Collections.Generic.List[object]
        $ms = @{}; $angles = @{}; $errors = @{}
        foreach ($k in $kinds) { $ms[$k] = 0.0; $angles[$k] = 0.0; $errors[$k] = '' }
        for ($ti = 0; $ti -lt $plan.Count; $ti++) {
            $crop = $plan[$ti]
            $scaled = New-ScaledCrop $src $crop $s
            $sx = $scaled.Width / [double]$crop[2]; $sy = $scaled.Height / [double]$crop[3]
            try {
                foreach ($kind in $kinds) {
                    $t0 = [Diagnostics.Stopwatch]::StartNew()
                    $res = $null
                    try {
                        $bmp = New-PassBitmap $scaled $kind
                        try {
                            $sb = ConvertTo-SoftwareBitmap $bmp
                            try {
                                $res = Await ($script:Engine.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])
                            } finally { $sb.Dispose() }
                        } finally { $bmp.Dispose() }
                    } catch {
                        # one failing pass (memory, codec) must not lose the words of the others
                        $errors[$kind] = $_.Exception.Message
                        $ms[$kind] += $t0.Elapsed.TotalMilliseconds
                        continue
                    }
                    if ($null -ne $res.TextAngle) { $angles[$kind] = [double]$res.TextAngle }
                    $pass = $kind + $suffix
                    $li = 0
                    foreach ($line in $res.Lines) {
                        foreach ($word in $line.Words) {
                            $box = Convert-Box $word.BoundingRect $crop $sx $sy $scaled.Width $scaled.Height $ImgW $ImgH
                            $found.Add([pscustomobject]@{
                                    Text = $word.Text; X = $box[0]; Y = $box[1]; W = $box[2]; H = $box[3]; Cut = $box[4]
                                    Pass = $pass; Line = ('{0}:{1}:{2}' -f $pass, $ti, $li); Bg = $null
                                })
                        }
                        $li++
                    }
                    $ms[$kind] += $t0.Elapsed.TotalMilliseconds
                }
            } finally { $scaled.Dispose() }
        }
        foreach ($k in $kinds) {
            $n = 0
            foreach ($f in $found) { if ($f.Pass -eq ($k + $suffix)) { $n++ } }
            $script:PassInfo.Add(('{{"name":{0},"scale":{1},"tiles":{2},"ms":{3},"words":{4},"angle":{5},"error":{6}}}' -f
                    (ConvertTo-JsonString ($k + $suffix)), (Format-Num $s), $plan.Count, [int]$ms[$k], $n,
                    (Format-Num $angles[$k]), (ConvertTo-JsonString $errors[$k])))
        }
        return , $found
    }

    $kinds = @($Passes.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $s = $Scale
    $tLoad = [int]$sw.Elapsed.TotalMilliseconds
    if ($s -le 0) {
        # Probe: white-mask pass at 2x, measure text height, pick the scale for ~TargetHeight px text.
        $s0 = 2.0
        $probe = Invoke-Passes $s0 @('white') '@probe'
        $s = Get-AutoScale (Get-MedianHeight $probe) $TargetHeight 3.0
        if ([Math]::Abs($s - $s0) / $s0 -lt 0.25) {
            $s = $s0
            foreach ($p in $probe) { $p.Pass = 'white'; $p.Line = $p.Line -replace '^white@probe', 'white' }
            $last = $script:PassInfo.Count - 1
            $script:PassInfo[$last] = $script:PassInfo[$last] -replace '"white@probe"', '"white"'
            $kinds = @($kinds | Where-Object { $_ -ne 'white' })
        }
        foreach ($p in $probe) { $script:Words.Add($p) }
    }
    if ($kinds.Count -gt 0) {
        foreach ($p in (Invoke-Passes $s $kinds '')) { $script:Words.Add($p) }
    }
    foreach ($p in $script:Words) { $p.Bg = Get-Background $p.X $p.Y $p.W $p.H }
    $src.Dispose()

    $sbOut = New-Object System.Text.StringBuilder
    [void]$sbOut.Append(('{{"ok":true,"width":{0},"height":{1},"scale":{2},"maxdim":{3},"lang":{4},"load_ms":{5},"ms":{6},"passes":[' -f
            $ImgW, $ImgH, (Format-Num $s), $maxDim, (ConvertTo-JsonString $script:Engine.RecognizerLanguage.LanguageTag),
            $tLoad, [int]$sw.Elapsed.TotalMilliseconds))
    [void]$sbOut.Append(($script:PassInfo -join ','))
    [void]$sbOut.Append('],"words":[')
    $first = $true
    foreach ($p in $script:Words) {
        if (-not $first) { [void]$sbOut.Append(',') }
        [void]$sbOut.Append((ConvertTo-WordJson $p))
        $first = $false
    }
    [void]$sbOut.Append(']}')
    Write-Result $sbOut.ToString()
    exit 0
} catch {
    $msg = ($_.Exception.Message + ' @ ' + $_.InvocationInfo.PositionMessage)
    Write-Result ('{"ok":false,"code":"ERROR","error":' + (ConvertTo-JsonString $msg) + '}')
    exit 2
}
"""


def recognize_regions(image_bytes, regions, suffix=".png", run=subprocess.run, dump_dir=None):
    """Повторно прочитать небольшие области (значки «xN»): {"words": [{"text", "region", "pass", "line"}]}.
    dump_dir — сохранить туда листы, которые читало распознавание (для проверки)."""
    spec = ";".join(",".join(str(int(v)) for v in region) for region in regions)
    extra = ["-Regions", spec] + (["-DumpDir", str(dump_dir)] if dump_dir else [])
    return recognize_words(image_bytes, suffix=suffix, extra=extra, run=run)


def recognize_words(image_bytes, suffix=".png", passes=PASSES, scale=0.0, run=subprocess.run, extra=None):
    """Скриншот -> {"width","height","scale","passes":[...],"words":[{"text","x","y","w","h",
    "pass","line","cut","bg"}]}. OcrError — с понятным пользователю сообщением."""
    if run is subprocess.run and not available():
        raise OcrError("распознавание скриншотов работает в Windows 10/11; вставьте список предметов текстом")
    try:
        workdir = tempfile.mkdtemp(prefix="mm2values-ocr-")
    except OSError as error:
        raise OcrError(f"не удалось создать временную папку: {error}") from error
    image = os.path.join(workdir, "shot" + (suffix if suffix.startswith(".") else ".png"))
    script = os.path.join(workdir, "tiles.ps1")
    try:
        try:
            with open(image, "wb") as file:
                file.write(image_bytes)
            with open(script, "w", encoding="utf-8-sig") as file:
                file.write(_TILE_SCRIPT)
        except OSError as error:
            raise OcrError(f"не удалось сохранить картинку во временную папку: {error}") from error
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", script, "-Path", image, "-Passes", passes]
        if scale:
            command += ["-Scale", "%.3f" % scale]
        command += list(extra or [])
        try:
            result = run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise OcrError("распознавание заняло слишком много времени — обрежьте скриншот до сетки предметов") from error
        except OSError as error:
            raise OcrError(f"распознавание не запустилось: {error}") from error
        try:
            data = tileocr.parse_output(result.stdout)
        except ValueError:
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise OcrError("не удалось распознать картинку: " + (message[-1] if message else "нет ответа")) from None
        if not data.get("ok"):
            if data.get("code") == "NO_OCR_ENGINE":
                raise OcrError("в Windows не установлен язык распознавания текста (Параметры → Время и язык → "
                               "Язык → добавить English (United States))")
            raise OcrError("не удалось распознать картинку: " + str(data.get("error", "ошибка")))
        failed = [p.get("error") for p in data.get("passes", []) if p.get("error")]
        if not data.get("words") and failed:
            raise OcrError("не удалось распознать картинку: " + failed[0])
        return data
    finally:
        for name in (image, script):
            try:
                os.unlink(name)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass


def read_inventory(image_bytes, matcher=None, known_names=None, run=subprocess.run):
    """Скриншот инвентаря MM2 -> (слова, плитки). Плитки с названием, но без значка
    «xN», читаются второй раз: угол плитки отдельно и крупно — короткие «x2» и «x3»
    распознаванию Windows на целой картинке часто не даются."""
    words = recognize_words(image_bytes, run=run)
    tiles = tileocr.recognize(words, known_names, matcher)
    regions = tileocr.badge_regions(tiles)
    if regions:
        try:
            again = recognize_regions(image_bytes, [area for _, area in regions], run=run)
            tileocr.apply_region_badges(tiles, regions, again.get("words"), again.get("areas"))
        except OcrError:
            pass
    return words, tiles


def lines_from_words(data):
    """Строки текста (для скриншотов не инвентаря: список, чат): по строкам самого
    «разговорчивого» варианта распознавания, слева направо."""
    by_pass = {}
    for word in data.get("words", []):
        by_pass.setdefault(str(word.get("pass", "")).split("@")[0], []).append(word)
    if not by_pass:
        return []
    words = max(by_pass.values(), key=lambda ws: sum(len(str(w.get("text", ""))) for w in ws))
    lines = {}
    for word in words:
        lines.setdefault(word.get("line", ""), []).append(word)
    ordered = sorted(lines.values(), key=lambda ws: (min(w["y"] for w in ws), min(w["x"] for w in ws)))
    return [" ".join(str(w["text"]) for w in sorted(ws, key=lambda w: w["x"])) for ws in ordered]


def recognize(image_bytes, suffix=".png"):
    """Строки текста со скриншота. OcrError — если распознать не удалось."""
    return [line for line in (l.strip() for l in lines_from_words(recognize_words(image_bytes, suffix))) if line]
