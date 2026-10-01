<#
.SYNOPSIS
Draw the Bitcoin icon offline and write api/static/icons/bitcoin.ico.

.DESCRIPTION
Windows only: uses .NET System.Drawing and the Segoe UI Bold font that ships
with Windows, which has the Bitcoin sign (U+20BF). Nothing is downloaded.
Each size is drawn from vectors at its own resolution, encoded as PNG, and
packed into one .ico. After changing the output, update the SHA-256 in
api/static/icons/README.md.

.PARAMETER PreviewDir
Optional folder for preview PNGs: the 256 px image as drawn, and the 16 and
32 px images enlarged eight times without smoothing.
#>
param(
    [string]$OutFile = (Join-Path $PSScriptRoot '..\api\static\icons\bitcoin.ico'),
    [string]$PreviewDir = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$Sizes = 16, 24, 32, 48, 64, 128, 256
$Orange = [System.Drawing.Color]::FromArgb(255, 0xF7, 0x93, 0x1A)
$Glyph = [string][char]0x20BF
$Tilt = 14  # degrees clockwise, as in the Bitcoin logo

# The glyph's share of the circle's diameter, ticks included. Tab-sized icons
# get a larger glyph so it stays legible.
function Get-GlyphHeight([int]$size) {
    if ($size -le 16) { return 0.74 }
    if ($size -le 24) { return 0.70 }
    return 0.64
}

function New-IconBitmap([int]$size) {
    $bitmap = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $family = New-Object System.Drawing.FontFamily 'Segoe UI'
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $matrix = New-Object System.Drawing.Drawing2D.Matrix
    $circle = New-Object System.Drawing.SolidBrush $Orange
    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.FillEllipse($circle, 0, 0, $size, $size)

        $path.AddString($Glyph, $family, [int][System.Drawing.FontStyle]::Bold, 100,
            (New-Object System.Drawing.PointF 0, 0), [System.Drawing.StringFormat]::GenericTypographic)
        $bounds = $path.GetBounds()
        $scale = ($size * (Get-GlyphHeight $size)) / $bounds.Height
        $append = [System.Drawing.Drawing2D.MatrixOrder]::Append
        $matrix.Translate(-($bounds.X + $bounds.Width / 2), -($bounds.Y + $bounds.Height / 2), $append)
        $matrix.Scale($scale, $scale, $append)
        $matrix.Rotate($Tilt, $append)
        $matrix.Translate($size / 2, $size / 2, $append)
        $path.Transform($matrix)
        $graphics.FillPath([System.Drawing.Brushes]::White, $path)
    }
    finally {
        $circle.Dispose(); $matrix.Dispose(); $path.Dispose(); $family.Dispose(); $graphics.Dispose()
    }
    return $bitmap
}

function ConvertTo-Png([System.Drawing.Bitmap]$bitmap) {
    $stream = New-Object System.IO.MemoryStream
    try {
        $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        return , $stream.ToArray()
    }
    finally {
        $stream.Dispose()
    }
}

function Save-Enlarged([System.Drawing.Bitmap]$bitmap, [int]$factor, [string]$file) {
    $side = $bitmap.Width * $factor
    $large = New-Object System.Drawing.Bitmap $side, $side, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($large)
    try {
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
        $graphics.DrawImage($bitmap, 0, 0, $side, $side)
        $large.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $graphics.Dispose(); $large.Dispose()
    }
}

$images = @()
foreach ($size in $Sizes) {
    $bitmap = New-IconBitmap $size
    try {
        $images += , @{ Size = $size; Png = (ConvertTo-Png $bitmap) }
        if ($PreviewDir) {
            New-Item -ItemType Directory -Force -Path $PreviewDir | Out-Null
            if ($size -eq 256) { $bitmap.Save((Join-Path $PreviewDir 'bitcoin-256.png'), [System.Drawing.Imaging.ImageFormat]::Png) }
            if ($size -eq 16 -or $size -eq 32) { Save-Enlarged $bitmap 8 (Join-Path $PreviewDir "bitcoin-$size-x8.png") }
        }
    }
    finally {
        $bitmap.Dispose()
    }
}

# ICO layout: a 6-byte header, one 16-byte entry per image, then the PNG data.
$ico = New-Object System.IO.MemoryStream
$writer = New-Object System.IO.BinaryWriter $ico
try {
    $writer.Write([uint16]0)
    $writer.Write([uint16]1)
    $writer.Write([uint16]$images.Count)
    $offset = 6 + 16 * $images.Count
    foreach ($image in $images) {
        $edge = if ($image.Size -ge 256) { 0 } else { $image.Size }  # 0 means 256
        $writer.Write([byte]$edge)
        $writer.Write([byte]$edge)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$image.Png.Length)
        $writer.Write([uint32]$offset)
        $offset += $image.Png.Length
    }
    foreach ($image in $images) {
        $writer.Write([byte[]]$image.Png)
    }
    $writer.Flush()
    $target = [System.IO.Path]::GetFullPath($OutFile)
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    [System.IO.File]::WriteAllBytes($target, $ico.ToArray())
}
finally {
    $writer.Dispose()
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash
Write-Output "Wrote $target ($((Get-Item -LiteralPath $target).Length) bytes)"
Write-Output "SHA-256: $hash"
