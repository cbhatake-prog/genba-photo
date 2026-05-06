param(
  [Parameter(Mandatory=$true)][string]$WavFile,
  [int]$TimeoutSeconds = 10
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine
try {
  $grammar = New-Object System.Speech.Recognition.DictationGrammar
  $engine.LoadGrammar($grammar)
  $engine.SetInputToWaveFile($WavFile)
  $texts = New-Object System.Collections.Generic.List[string]
  $alternates = New-Object System.Collections.Generic.List[string]
  $confSum = 0.0
  $count = 0
  for ($i = 0; $i -lt 20; $i++) {
    try {
      $result = $engine.Recognize([TimeSpan]::FromSeconds($TimeoutSeconds))
    } catch {
      if ($count -gt 0 -and $_.Exception.Message -match 'No audio input') {
        break
      }
      throw
    }
    if ($null -eq $result) { break }
    if ($result.Text) {
      [void]$texts.Add($result.Text)
      $confSum += $result.Confidence
      $count += 1
    }
    foreach ($alt in @($result.Alternates | Select-Object -First 8)) {
      if ($alt.Text) { [void]$alternates.Add($alt.Text) }
    }
  }
  if ($count -eq 0) {
    [pscustomobject]@{
      text = ''
      confidence = 0
      alternates = @()
      error = $null
    } | ConvertTo-Json -Compress
  } else {
    [pscustomobject]@{
      text = ($texts -join ' ')
      confidence = ($confSum / $count)
      alternates = @($alternates | Select-Object -Unique | Select-Object -First 20)
      error = $null
    } | ConvertTo-Json -Compress
  }
} catch {
  [pscustomobject]@{
    text = ''
    confidence = 0
    alternates = @()
    error = $_.Exception.Message
  } | ConvertTo-Json -Compress
  exit 1
} finally {
  if ($engine) { $engine.Dispose() }
}
