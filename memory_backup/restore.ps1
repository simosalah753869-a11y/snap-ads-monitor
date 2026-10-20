$ErrorActionPreference = 'Stop'
$token = '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'
$owner = 'simosalah753869-a11y'
$repo  = 'snap-ads-monitor'
$headers = @{
    'Authorization' = "Bearer $token"
    'Accept'        = 'application/vnd.github.raw+json'
    'User-Agent'    = 'Antigravity-Dell-Restore'
}

Write-Host '🚀 جاري تحميل فهرس الذاكرة من GitHub...' -ForegroundColor Cyan
$manifestUrl = "https://api.github.com/repos/$owner/$repo/contents/memory_backup/manifest.json"
$manifest = Invoke-RestMethod -Uri $manifestUrl -Headers $headers
$zipPath = Join-Path $HOME 'antigravity_memory_restore.zip'
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

$fs = [System.IO.File]::OpenWrite($zipPath)
try {
    foreach ($part in $manifest.parts) {
        Write-Host "⬇️ جاري تحميل الجزء: $part ..." -ForegroundColor Yellow
        $partUrl = "https://api.github.com/repos/$owner/$repo/contents/$part"
        $tmpPart = Join-Path $HOME 'part_tmp.bin'
        Invoke-WebRequest -Uri $partUrl -Headers $headers -OutFile $tmpPart -UseBasicParsing
        $bytes = [System.IO.File]::ReadAllBytes($tmpPart)
        $fs.Write($bytes, 0, $bytes.Length)
        Remove-Item $tmpPart -Force -ErrorAction SilentlyContinue
    }
} finally {
    $fs.Close()
}

Write-Host '📦 اكتمل التحميل! جاري فك ضغط الذاكرة والمهارات والسكريبتات...' -ForegroundColor Cyan
Expand-Archive -Path $zipPath -DestinationPath $HOME -Force
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

if ($HOME -ne 'C:\Users\salah') {
    Write-Host "🔧 مواءمة المسارات تلقائياً مع حساب المستخدم في حاسوب ديل ($HOME)..." -ForegroundColor Cyan
    $files = Get-ChildItem -Path (Join-Path $HOME '.gemini') -Recurse -File -Include *.md,*.json,*.py,*.txt,*.bat,*.ps1 -ErrorAction SilentlyContinue
    $old1 = 'C:\Users\salah'
    $old2 = 'C:\\Users\\salah'
    $old3 = 'C:/Users/salah'
    $new1 = $HOME
    $new2 = $HOME.Replace('\', '\\')
    $new3 = $HOME.Replace('\', '/')
    foreach ($f in $files) {
        try {
            $c = [System.IO.File]::ReadAllText($f.FullName)
            $u = $c.Replace($old2, $new2).Replace($old1, $new1).Replace($old3, $new3)
            if ($u -ne $c) { [System.IO.File]::WriteAllText($f.FullName, $u, [System.Text.Encoding]::UTF8) }
        } catch {}
    }
    if (-not (Test-Path 'C:\Users\salah')) {
        cmd /c mklink /J "C:\Users\salah" "$HOME" 2>$null | Out-Null
    }
}

Write-Host '🎉 تمت استعادة الذاكرة الكاملة والمهارات والسكريبتات وصور المنتج في حاسوب ديل بنجاح 100%!' -ForegroundColor Green
