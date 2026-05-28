$ErrorActionPreference = 'Continue'
$log = 'docker_push_output.txt'

function utc { (Get-Date).ToUniversalTime().ToString("HH:mm 'UTC'") }

"" | Out-File $log -Encoding utf8
"=== Docker Build + Push v3.23 ===" | Out-File $log -Append -Encoding utf8
"Started:  $(utc)" | Out-File $log -Append -Encoding utf8
"" | Out-File $log -Append -Encoding utf8

# Build
"[$(utc)] Building image (this usually takes 3-8 min)..." | Out-File $log -Append -Encoding utf8
$build = Start-Process -FilePath "docker" `
    -ArgumentList "build", "-t", "sushi0934/transcript-agent:v3.23", "-t", "sushi0934/transcript-agent:latest", "." `
    -PassThru -Wait -NoNewWindow `
    -RedirectStandardOutput "docker_build_stdout.txt" `
    -RedirectStandardError  "docker_build_stderr.txt"

"[$(utc)] Build exit code: $($build.ExitCode)" | Out-File $log -Append -Encoding utf8
if ($build.ExitCode -ne 0) {
    "BUILD FAILED - stderr below:" | Out-File $log -Append -Encoding utf8
    Get-Content "docker_build_stderr.txt" -Tail 30 | Out-File $log -Append -Encoding utf8
    exit 1
}
"[$(utc)] Build SUCCESS" | Out-File $log -Append -Encoding utf8

# Push v3.23
"[$(utc)] Pushing v3.23 tag..." | Out-File $log -Append -Encoding utf8
$p1 = Start-Process -FilePath "docker" `
    -ArgumentList "push", "sushi0934/transcript-agent:v3.23" `
    -PassThru -Wait -NoNewWindow `
    -RedirectStandardOutput "docker_push_v310_stdout.txt" `
    -RedirectStandardError  "docker_push_v310_stderr.txt"
"[$(utc)] Push v3.23 exit code: $($p1.ExitCode)" | Out-File $log -Append -Encoding utf8

# Push latest
"[$(utc)] Pushing latest tag..." | Out-File $log -Append -Encoding utf8
$p2 = Start-Process -FilePath "docker" `
    -ArgumentList "push", "sushi0934/transcript-agent:latest" `
    -PassThru -Wait -NoNewWindow `
    -RedirectStandardOutput "docker_push_latest_stdout.txt" `
    -RedirectStandardError  "docker_push_latest_stderr.txt"
"[$(utc)] Push latest exit code: $($p2.ExitCode)" | Out-File $log -Append -Encoding utf8

"" | Out-File $log -Append -Encoding utf8
if ($p1.ExitCode -eq 0 -and $p2.ExitCode -eq 0) {
    "=== ALL DONE at $(utc) ===" | Out-File $log -Append -Encoding utf8
    "Tags pushed: v3.23 + latest" | Out-File $log -Append -Encoding utf8
} else {
    "=== PUSH FAILED at $(utc) ===" | Out-File $log -Append -Encoding utf8
}
