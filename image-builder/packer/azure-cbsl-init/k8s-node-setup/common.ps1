$global:KUBERNETES_DIR = Join-Path $env:SystemDrive "k"
$global:CONTAINERD_DIR = Join-Path $env:SystemDrive "containerd"
$global:VAR_LOG_DIR = Join-Path $env:SystemDrive "var\log"
$global:VAR_LIB_DIR = Join-Path $env:SystemDrive "var\lib"
$global:ETC_DIR = Join-Path $env:SystemDrive "etc"
$global:NSSM_DIR = Join-Path $env:ProgramFiles "nssm"
$global:OPT_DIR = Join-Path $env:SystemDrive "opt"

$global:KUBERNETES_PAUSE_IMAGE = "mcr.microsoft.com/oss/kubernetes/pause:1.4.1"

$global:CNI_PLUGINS_VERSION = "0.9.1"
$global:WINDOWS_CNI_PLUGINS_VERSION = "0.2.0"
$global:WINS_VERSION = "0.1.1"
$global:FLANNEL_VERSION = "0.14.0"
$global:CRI_CONTAINERD_VERSION = "1.5.2"
$global:CRICTL_VERSION = "1.21.0"

$global:NSSM_URL = "https://k8stestinfrabinaries.blob.core.windows.net/nssm-mirror/nssm-2.24.zip"


function Start-ExecuteWithRetry {
    Param(
        [Parameter(Mandatory=$true)]
        [ScriptBlock]$ScriptBlock,
        [int]$MaxRetryCount=10,
        [int]$RetryInterval=3,
        [string]$RetryMessage,
        [array]$ArgumentList=@()
    )
    $currentErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $retryCount = 0
    while ($true) {
        try {
            $res = Invoke-Command -ScriptBlock $ScriptBlock `
                                  -ArgumentList $ArgumentList
            $ErrorActionPreference = $currentErrorActionPreference
            return $res
        } catch [System.Exception] {
            $retryCount++
            if ($retryCount -gt $MaxRetryCount) {
                $ErrorActionPreference = $currentErrorActionPreference
                throw
            } else {
                if($RetryMessage) {
                    Write-Output "Retry (${retryCount}/${MaxRetryCount}): $RetryMessage"
                } elseif($_) {
                    Write-Output "Retry (${retryCount}/${MaxRetryCount}): $_"
                }
                Start-Sleep $RetryInterval
            }
        }
    }
}

function Start-FileDownload {
    Param(
        [Parameter(Mandatory=$true)]
        [string]$URL,
        [Parameter(Mandatory=$true)]
        [string]$Destination,
        [Parameter(Mandatory=$false)]
        [int]$RetryCount=10
    )
    Write-Output "Downloading $URL to $Destination"
    Start-ExecuteWithRetry -ScriptBlock {
        Invoke-Expression "curl.exe -L -s -o $Destination $URL"
    } -MaxRetryCount $RetryCount -RetryInterval 3 -RetryMessage "Failed to download $URL. Retrying"
}

function Add-ToSystemPath {
    Param(
        [Parameter(Mandatory=$false)]
        [string[]]$Path
    )

    if(!$Path) {
        return
    }

    $systemPath = [Environment]::GetEnvironmentVariable("PATH", [System.EnvironmentVariableTarget]::Machine).Split(';')
    $currentPath = $env:PATH.Split(';')
    foreach($p in $Path) {
        if($p -notin $systemPath) {
            $systemPath += $p
        }
        if($p -notin $currentPath) {
            $currentPath += $p
        }
    }

    $env:PATH = $currentPath -join ';'
    $newSystemPath = $systemPath -join ';'
    [Environment]::SetEnvironmentVariable("PATH", $newSystemPath, [System.EnvironmentVariableTarget]::Machine)
}

function Get-WindowsRelease {
    $releases = @{
        17763 = "ltsc2019"
        18363 = "1909"
        19041 = "2004"
    }
    $osBuild = [System.Environment]::OSVersion.Version.Build
    $releaseName = $releases[$osBuild]
    if (!$releaseName) {
        Throw "Cannot find the Windows release name"
    }
    return $releaseName
}

function Get-NanoServerImage {
    $release = Get-WindowsRelease
    if($release -eq "ltsc2019") {
        $release = "1809"
    }
    return "mcr.microsoft.com/windows/nanoserver:$release"
}

function Install-NSSM {
    Write-Output "Installing NSSM"
    mkdir -Force $NSSM_DIR

    Start-FileDownload $NSSM_URL "$env:TEMP\nssm.zip"
    tar -C "$NSSM_DIR" -xvf $env:TEMP\nssm.zip --strip-components 2 */win64/*.exe
    if($LASTEXITCODE) {
        Throw "Failed to unzip nssm.zip"
    }

    Remove-Item -Force "$env:TEMP\nssm.zip"
    Add-ToSystemPath $NSSM_DIR
}

function Install-Kubelet {
    Param(
        [parameter(Mandatory=$true)]
        [string]$KubernetesVersion,
        [parameter(Mandatory=$true)]
        [string]$StartKubeletScriptPath,
        [parameter(Mandatory=$true)]
        [string]$ContainerRuntimeServiceName
    )

    mkdir -force $KUBERNETES_DIR
    Add-ToSystemPath $KUBERNETES_DIR

    Start-FileDownload "https://dl.k8s.io/$KubernetesVersion/bin/windows/amd64/kubelet.exe" "$KUBERNETES_DIR\kubelet.exe"
    Start-FileDownload "https://dl.k8s.io/$KubernetesVersion/bin/windows/amd64/kubeadm.exe" "$KUBERNETES_DIR\kubeadm.exe"
    Start-FileDownload "https://github.com/rancher/wins/releases/download/v${WINS_VERSION}/wins.exe" "$KUBERNETES_DIR\wins.exe"

    Write-Output "Registering wins Windows service"
    wins.exe srv app run --register
    if($LASTEXITCODE) {
        Throw "Failed to register wins Windows service"
    }
    Start-Service rancher-wins

    mkdir -force "$VAR_LOG_DIR\kubelet"
    mkdir -force "$VAR_LIB_DIR\kubelet\etc\kubernetes"
    mkdir -force "$VAR_LIB_DIR\kubelet\etc\kubernetes\manifests"
    mkdir -force "$ETC_DIR\kubernetes\pki"
    New-Item -Path "$VAR_LIB_DIR\kubelet\etc\kubernetes\pki" -Type SymbolicLink `
             -Value "$ETC_DIR\kubernetes\pki"
    Copy-Item -Force -Path $StartKubeletScriptPath -Destination "$KUBERNETES_DIR\StartKubelet.ps1"

    Write-Output "Registering kubelet service"

    nssm install kubelet (Get-Command powershell).Source "-ExecutionPolicy Bypass -NoProfile" $KUBERNETES_DIR\StartKubelet.ps1
    if($LASTEXITCODE) {
        Throw "Failed to register kubelet Windows service"
    }
    nssm set kubelet DependOnService $ContainerRuntimeServiceName
    if($LASTEXITCODE) {
        Throw "Failed to set kubelet DependOnService"
    }
    nssm set kubelet AppEnvironmentExtra K8S_PAUSE_IMAGE=$KUBERNETES_PAUSE_IMAGE
    if($LASTEXITCODE) {
        Throw "Failed to set kubelet K8S_PAUSE_IMAGE nssm extra env variable"
    }
    nssm set kubelet Start SERVICE_DEMAND_START
    if($LASTEXITCODE) {
        Throw "Failed to set kubelet manual start type"
    }

    New-NetFirewallRule -Name "kubelet" -DisplayName "kubelet" -Enabled True `
                        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 10250
}

function Install-ContainerNetworkingPlugins {
    mkdir -force "$OPT_DIR\cni\bin"

    Start-FileDownload "https://github.com/containernetworking/plugins/releases/download/v${CNI_PLUGINS_VERSION}/cni-plugins-windows-amd64-v${CNI_PLUGINS_VERSION}.tgz" `
                       "$env:TEMP\cni-plugins.tgz"
    tar -xf $env:TEMP\cni-plugins.tgz -C "$OPT_DIR\cni\bin"
    if($LASTEXITCODE) {
        Throw "Failed to extract cni-plugins.tgz"
    }
    Remove-Item -Force $env:TEMP\cni-plugins.tgz
}

function Get-ContainerImages {
    Param(
        [parameter(Mandatory=$true)]
        [string]$ContainerRegistry,
        [parameter(Mandatory=$true)]
        [string]$KubernetesVersion
    )

    $windowsRelease = Get-WindowsRelease
    return @(
        $KUBERNETES_PAUSE_IMAGE,
        (Get-NanoServerImage),
        "mcr.microsoft.com/windows/servercore:${windowsRelease}",
        "${ContainerRegistry}/flannel-windows:v${FLANNEL_VERSION}-windowsservercore-${windowsRelease}",
        "${ContainerRegistry}/kube-proxy-windows:${KubernetesVersion}-windowsservercore-${windowsRelease}"
    )
}
