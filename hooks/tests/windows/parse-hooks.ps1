# windows-ci: ps51 ps7 | Parse every hook script
# Parse every hook script with PowerShell's own parser, without running any of
# it. The Macs that write these files have no PowerShell, so a typo or a quoting
# slip used to travel all the way to a builder's PC before anyone saw it.
# Run by .github/workflows/windows-hooks.yml under both Windows PowerShell 5.1
# and PowerShell 7; also fine to run by hand on any PC.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$bad = 0
# install.ps1 sits beside hooks/, not in it, and was never parsed here.
$installer = Join-Path (Split-Path $root) 'install.ps1'
@(Get-ChildItem -Path $root -Filter *.ps1 -Recurse) + @(Get-Item $installer -ErrorAction SilentlyContinue) |
    Sort-Object FullName | ForEach-Object {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    # ASCII only: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so one
    # UTF-8 character (an em dash in a comment, in #211) is mis-decoded on a PC.
    # Byte check, so it doesn't depend on how this shell decoded the file.
    $lineNo = 1
    $firstBad = 0
    foreach ($b in [System.IO.File]::ReadAllBytes($_.FullName)) {
        if ($b -eq 10) { $lineNo++ }
        elseif ($b -gt 127) { $firstBad = $lineNo; break }
    }
    if ($firstBad -gt 0) {
        $bad++
        Write-Host ("FAIL {0}:{1}: non-ASCII character (5.1 reads this file as ANSI)" -f $_.FullName, $firstBad)
    }
    if ($errors.Count -gt 0) {
        $bad += $errors.Count
        foreach ($e in $errors) {
            Write-Host ("FAIL {0}:{1}: {2}" -f $_.FullName, $e.Extent.StartLineNumber, $e.Message)
        }
    } elseif ($firstBad -eq 0) {
        Write-Host ("ok   {0} (PowerShell {1})" -f $_.Name, $PSVersionTable.PSVersion)
    }
}
if ($bad -gt 0) {
    Write-Host "$bad problem(s): parse errors or non-ASCII files"
    exit 1
}
