param(
    [Parameter(Mandatory)] [string] $ServerAddress,
    [Parameter(Mandatory)] [string] $RuntimeDirectory
)

$ErrorActionPreference = 'Stop'
$RuntimeDirectory = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$identity = Join-Path $RuntimeDirectory 'id_ed25519_gateway'
$knownHosts = Join-Path $RuntimeDirectory 'known_hosts'
foreach ($file in @($identity, $knownHosts)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Required SSH file is missing: $file"
    }
}

$lockPath = Join-Path $RuntimeDirectory 'tunnel.lock'
$lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'Write', 'None')
$ssh = (Get-Command ssh.exe).Source
$arguments = @(
    '-F', 'NUL', '-N', '-T', '-i', $identity,
    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
    '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$knownHosts",
    '-o', 'GlobalKnownHostsFile=NUL', '-o', 'UpdateHostKeys=no',
    '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
    '-o', 'ServerAliveCountMax=2',
    '-R', '127.0.0.1:18080:127.0.0.1:8001', "iikuznitsa-tunnel@$ServerAddress"
)

try {
    while ($true) {
        Write-Output "$(Get-Date -Format o) Connecting to Selectel"
        & $ssh @arguments
        Write-Output "$(Get-Date -Format o) SSH exited with $LASTEXITCODE. Reconnecting in 5 seconds."
        Start-Sleep -Seconds 5
    }
} finally {
    $lock.Dispose()
}
