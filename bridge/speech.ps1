param([Parameter(Mandatory=$true)][string]$WavePath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$engine = [System.Speech.Recognition.SpeechRecognitionEngine]::new([Globalization.CultureInfo]::GetCultureInfo('zh-CN'))
try {
    $engine.LoadGrammar([System.Speech.Recognition.DictationGrammar]::new())
    $engine.SetInputToWaveFile($WavePath)
    $parts = [Collections.Generic.List[string]]::new()
    while ($true) {
        try { $result = $engine.Recognize([TimeSpan]::FromSeconds(10)) }
        catch {
            # SAPI detaches a WAV source at EOF; a further Recognize may throw.
            if ($parts.Count -gt 0 -and $_.Exception.InnerException -is [InvalidOperationException]) { break }
            throw
        }
        if ($null -eq $result) { break }
        if ($result.Text) { $parts.Add($result.Text) }
    }
    [Console]::Write(($parts -join ''))
} finally { $engine.Dispose() }
