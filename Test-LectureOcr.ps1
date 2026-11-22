# Run with Windows PowerShell 5.1. The PDF and rendered slide stay in memory.
param([ValidateRange(1, 10000)][int]$Page = 2)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Web
Add-Type -AssemblyName System.Net.Http
$null = [Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]

$extensions = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1
}
$asTask = $extensions | Where-Object {
    $_.IsGenericMethod -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}
$asAction = $extensions | Where-Object {
    -not $_.IsGenericMethod -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
}
function Wait-Result($operation, [type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}

$language = [Windows.Globalization.Language]::new('ru-RU')
$ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if (-not $ocr) { throw 'Russian Windows OCR is unavailable.' }
$address = [uri](Read-Host 'PDF link').Trim()
if ($address.AbsolutePath -eq '/away.php') {
    $address = [uri][Web.HttpUtility]::ParseQueryString($address.Query)['to']
}
if ($address.Scheme -ne 'https' -or $address.Host -notin @('vk.com','vk.ru')) {
    throw 'Expected the VK document link copied from the lecture post.'
}
$client = [Net.Http.HttpClient]::new()
try {
    $client.Timeout = [TimeSpan]::FromSeconds(45)
    $bytes = $client.GetByteArrayAsync($address).GetAwaiter().GetResult()
    if ([Text.Encoding]::ASCII.GetString($bytes, 0, 5) -ne '%PDF-') { throw 'Expected PDF bytes.' }
    $source = [IO.MemoryStream]::new($bytes)
    $inputStream = [IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($source)
    $pdf = Wait-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromStreamAsync($inputStream)) ([Windows.Data.Pdf.PdfDocument])
    if ($Page -gt $pdf.PageCount) { throw 'Requested page exceeds page count.' }
    $slide = $pdf.GetPage($Page - 1)
    $rendered = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    $options = [Windows.Data.Pdf.PdfPageRenderOptions,Windows.Data.Pdf,ContentType=WindowsRuntime]::new()
    $options.DestinationWidth = [uint32][Math]::Min(2200, [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
    $action = $slide.RenderToStreamAsync($rendered, $options)
    $asAction.Invoke($null, @($action)).GetAwaiter().GetResult() | Out-Null
    $rendered.Seek(0)
    $decoder = Wait-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($rendered)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Wait-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime])
    $result = Wait-Result ($ocr.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime])
    "Page: $Page/$($pdf.PageCount)"
    "OCR characters: $($result.Text.Length)"
    "Sample: $($result.Text.Substring(0, [Math]::Min(350, $result.Text.Length)))"
} finally {
    $client.Dispose()
}
