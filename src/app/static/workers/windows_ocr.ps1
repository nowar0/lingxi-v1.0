param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Foundation.IAsyncAction,Windows.Foundation,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Length -eq 1
} | Select-Object -First 1
$asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and !$_.IsGenericMethod -and $_.GetParameters().Length -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
} | Select-Object -First 1
function Await-Result($Operation, [Type]$ResultType) {
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (!$task.Wait(25000)) { throw 'Windows OCR timeout' }
    return $task.Result
}
function Await-Action($Operation) {
    $task = $asTaskAction.Invoke($null, @($Operation))
    if (!$task.Wait(25000)) { throw 'Windows PDF render timeout' }
}
function Read-Page($Stream, $Engine, $Number) {
    $Stream.Seek(0)
    $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($Stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    if ($decoder.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
        $decoder.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) { throw 'Image exceeds Windows OCR dimensions' }
    $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
        $result = Await-Result ($Engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime])
        return @{page=$Number; lines=@($result.Lines | ForEach-Object {
            @{text=$_.Text;words=@($_.Words | ForEach-Object {
                @{text=$_.Text;x=$_.BoundingRect.X;y=$_.BoundingRect.Y;w=$_.BoundingRect.Width;h=$_.BoundingRect.Height}
            })}
        })}
    } finally { $bitmap.Dispose() }
}
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object {$_.LanguageTag})
if ($request.action -eq 'languages') {
    @{languages=$languages} | ConvertTo-Json -Compress
    exit 0
}
$lang = [Windows.Globalization.Language]::new($request.language)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if (!$engine) { throw 'OCR language is not installed in Windows' }
$file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($request.path)) ([Windows.Storage.StorageFile])
if ($file.FileType -ieq '.pdf') {
    $pdf = Await-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
    $pages = @($request.pages)
    if (!$pages -or $pages.Count -gt 8 -or ($pages | Where-Object {$_ -lt 1 -or $_ -gt $pdf.PageCount})) { throw 'Invalid PDF page selection' }
    $results = @(
        foreach ($number in $pages) {
            $page = $pdf.GetPage([uint32]($number - 1))
            $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
            try {
                $options = [Windows.Data.Pdf.PdfPageRenderOptions,Windows.Data.Pdf,ContentType=WindowsRuntime]::new()
                $options.DestinationWidth = [uint32][Math]::Min(2200,[Math]::Max(1000,$page.Size.Width * 2))
                $options.DestinationHeight = [uint32][Math]::Min(2600,[Math]::Round($options.DestinationWidth * $page.Size.Height / $page.Size.Width))
                Await-Action ($page.RenderToStreamAsync($stream, $options))
                Read-Page $stream $engine $number
            } finally { $stream.Dispose(); $page.Dispose() }
        }
    )
} else {
    $stream = Await-Result ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType,Windows.Storage.Streams,ContentType=WindowsRuntime])
    try { $results = @(Read-Page $stream $engine 1) } finally { $stream.Dispose() }
}
@{language=$request.language;pages=$results} | ConvertTo-Json -Depth 10 -Compress
