param(
    [string]$TextFile,
    [string]$OutFile,
    [int]$Rate = 0,
    [string]$Culture = "ko-KR",
    [string]$VoiceName = "",
    [switch]$ListOnly
)
# Windows 내장 음성(System.Speech)으로 텍스트 파일을 wav로 합성한다. 인터넷·추가 설치가 필요 없다.
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($ListOnly) {
        $synth.GetInstalledVoices() | Where-Object { $_.Enabled } |
            ForEach-Object { "{0}|{1}" -f $_.VoiceInfo.Name, $_.VoiceInfo.Culture.Name }
        exit 0
    }
    if ($VoiceName) {
        $synth.SelectVoice($VoiceName)
    } else {
        $voice = $synth.GetInstalledVoices() |
            Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq $Culture } | Select-Object -First 1
        if (-not $voice) { [Console]::Error.WriteLine("no voice for $Culture"); exit 2 }
        $synth.SelectVoice($voice.VoiceInfo.Name)
    }
    $synth.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
    $text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
    $synth.SetOutputToWaveFile($OutFile)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
