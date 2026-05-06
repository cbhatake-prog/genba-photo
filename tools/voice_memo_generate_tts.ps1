param(
  [Parameter(Mandatory=$true)][string]$Text,
  [Parameter(Mandatory=$true)][string]$OutFile,
  [int]$Rate = 0
)

$ErrorActionPreference = 'Stop'
$voice = New-Object -ComObject SAPI.SpVoice
$voices = $voice.GetVoices()
for ($i = 0; $i -lt $voices.Count; $i++) {
  $candidate = $voices.Item($i)
  if ($candidate.GetAttribute('Language') -eq '411') {
    $voice.Voice = $candidate
    break
  }
}
$voice.Rate = $Rate

$stream = New-Object -ComObject SAPI.SpFileStream
$format = New-Object -ComObject SAPI.SpAudioFormat
$format.Type = 39 # SAFT16kHz16BitMono
$stream.Format = $format

if (Test-Path -LiteralPath $OutFile) {
  Remove-Item -LiteralPath $OutFile -Force
}
$stream.Open($OutFile, 3, $false)
$voice.AudioOutputStream = $stream
[void]$voice.Speak($Text, 0)
$stream.Close()
