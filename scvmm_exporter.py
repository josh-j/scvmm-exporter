"""Minimal Prometheus exporter for System Center Virtual Machine Manager.

One PowerShell query runs over WinRM against the VMM management server on a
fixed interval; /metrics serves the last snapshot. Scrapes therefore never
block on SCVMM -- a slow or unreachable VMM server surfaces as `scvmm_up 0`
rather than as a Prometheus scrape timeout. A failed query drops the snapshot
instead of serving it stale, so no VM, host or cluster gauge ever outlives the
fleet state it described.
"""

import base64
import hashlib
import json
import logging
import os
import sys
import threading
import time

import winrm
from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily
from winrm.exceptions import InvalidCredentialsError

MB = 1024 * 1024

# Everything the exporter knows, in one round trip. The VMM cmdlets are the
# only supported read path (the SQL schema is not a contract), and each one is
# expensive enough that asking per metric would cost more than the whole scrape
# budget. Property access on a missing property yields $null here -- StrictMode
# is deliberately not set -- so a property this VMM build does not carry
# degrades to 0/"" rather than failing the whole query.
PS_QUERY = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module virtualmachinemanager
$vmm = Get-SCVMMServer -ComputerName localhost

$vms = @(Get-SCVirtualMachine -VMMServer $vmm | ForEach-Object {
  [pscustomobject]@{
    name          = [string]$_.Name
    host          = [string]$_.VMHost.Name
    cloud         = [string]$_.Cloud.Name
    os            = [string]$_.OperatingSystem.Name
    status        = [string]$_.Status
    cpu           = [double]$_.CPUCount
    memory_mb     = [double]$_.Memory
    storage_bytes = [double]$_.TotalSize
  }
})

$vmhosts = @(Get-SCVMHost -VMMServer $vmm | ForEach-Object {
  $volumes = @($_.DiskVolumes)
  [pscustomobject]@{
    name                   = [string]$_.Name
    cluster                = [string]$_.HostCluster.Name
    state                  = [string]$_.OverallState
    computer_state         = [string]$_.ComputerState
    hypervisor             = [string]$_.HyperVVersion
    cpu_percent            = [double]$_.CpuUtilization
    memory_total_bytes     = [double]$_.TotalMemory
    memory_available_mb    = [double]$_.AvailableMemory
    vm_count               = [double]@($_.VMs).Count
    storage_capacity_bytes = [double](@($volumes) |
                               Measure-Object -Property Capacity -Sum).Sum
    storage_free_bytes     = [double](@($volumes) |
                               Measure-Object -Property FreeSpace -Sum).Sum
  }
})

# Cluster health as VMM sees it: the cluster's own state, every node's
# membership and reachability, and the cluster shared volumes -- a CSV filling
# up is the failure this lab actually hits, and it is invisible from the
# per-host DiskVolumes above.
$clusters = @(Get-SCVMHostCluster -VMMServer $vmm | ForEach-Object {
  $cluster = $_
  $nodes = @($cluster.Nodes | ForEach-Object {
    [pscustomobject]@{
      name           = [string]$_.Name
      state          = [string]$_.OverallState
      computer_state = [string]$_.ComputerState
      cluster_state  = [string]$_.ClusterNodeStatus
    }
  })
  $volumes = @($cluster.SharedVolumes | ForEach-Object {
    [pscustomobject]@{
      name       = [string]$(if ($_.Name) { $_.Name } else { $_.VolumeLabel })
      capacity   = [double]$_.Capacity
      free       = [double]$_.FreeSpace
      accessible = [bool]$(if ($null -eq $_.IsAvailableForPlacement) `
                            { $true } else { $_.IsAvailableForPlacement })
    }
  })
  [pscustomobject]@{
    name             = [string]$cluster.Name
    state            = [string]$cluster.ClusterState
    overall_state    = [string]$cluster.OverallState
    validation_state = [string]$cluster.ClusterValidationState
    quorum           = [string]$cluster.QuorumConfiguration
    virtualization   = [string]$cluster.VirtualizationPlatform
    nodes            = $nodes
    volumes          = $volumes
  }
})

# -Newest bounds the job table, which grows without limit on a long-lived VMM
# server. Older builds that reject the parameter fall back to the full table.
$jobs = @()
try {
  $jobs = @(Get-SCJob -VMMServer $vmm -Newest 200)
} catch {
  try { $jobs = @(Get-SCJob -VMMServer $vmm) } catch { $jobs = @() }
}
$jobsummary = @{}
foreach ($job in $jobs) {
  $status = [string]$job.Status
  if (-not $status) { $status = 'Unknown' }
  if ($jobsummary.ContainsKey($status)) {
    $jobsummary[$status] = $jobsummary[$status] + 1
  } else {
    $jobsummary[$status] = 1
  }
}

[pscustomobject]@{
  server   = [string]$vmm.Name
  vms      = $vms
  vmhosts  = $vmhosts
  clusters = $clusters
  jobs     = $jobsummary
} | ConvertTo-Json -Depth 6 -Compress
"""

# The strings VMM uses for "this is fine", lowercased. Anything else is
# reported as unhealthy rather than guessed at.
HEALTHY_STATES = frozenset(["ok", "nominal", "running", "up", "healthy", "responding"])


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit("%s must be set" % name)
    return value


def as_list(value):
    """PowerShell serialises a one-element array as a bare object."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def text(value):
    return "" if value is None else str(value)


def healthy(value):
    return 1.0 if text(value).lower() in HEALTHY_STATES else 0.0


# Sentinel exit code the runner uses to say "the script file is not there";
# picked from the 200s to stay clear of PowerShell's own exit codes.
REMOTE_SCRIPT_MISSING = 213


class Scvmm:
    """The WinRM side: one PowerShell query, decoded."""

    def __init__(self, host, port, user, password, timeout):
        self.session = winrm.Session(
            "http://%s:%d/wsman" % (host, port),
            auth=(user, password),
            transport="ntlm",
            operation_timeout_sec=timeout,
            read_timeout_sec=timeout + 10,
        )
        # run_ps ships its script base64-encoded on the winrs command line,
        # which Windows caps around 8k characters -- PS_QUERY is past that
        # ("The command line is too long"). So the query lives in a remote
        # file instead: uploaded once as plain-base64 chunks (no quoting
        # hazards), decoded remotely, executed by path ever after. The name
        # carries a content hash so an edited query never runs a stale file.
        digest = hashlib.sha256(PS_QUERY.encode("utf-8")).hexdigest()[:12]
        self._remote_script = r"C:\Windows\Temp\scvmm-exporter-%s.ps1" % digest

    def _run_small(self, script):
        result = self.session.run_ps(script)
        if result.status_code != 0:
            error = result.std_err.decode("utf-8", "replace").strip()
            raise RuntimeError(error[:500] or "powershell exited %d" % result.status_code)
        return result

    def _upload_script(self):
        payload = base64.b64encode(PS_QUERY.encode("utf-8")).decode("ascii")
        b64_path = self._remote_script + ".b64"
        self._run_small("Set-Content -Path '%s' -Value '' -NoNewline" % b64_path)
        chunk = 2000
        for i in range(0, len(payload), chunk):
            self._run_small(
                "Add-Content -Path '%s' -Value '%s' -NoNewline"
                % (b64_path, payload[i:i + chunk])
            )
        self._run_small(
            "[IO.File]::WriteAllText('%s', [Text.Encoding]::UTF8.GetString("
            "[Convert]::FromBase64String((Get-Content '%s' -Raw))));"
            "Remove-Item '%s'" % (self._remote_script, b64_path, b64_path)
        )

    def query(self):
        # Check-and-run in one round trip; Windows temp cleanup may remove
        # the file at any time, so absence is an expected, recoverable state.
        runner = "if (Test-Path '%s') { & '%s' } else { exit %d }" % (
            self._remote_script,
            self._remote_script,
            REMOTE_SCRIPT_MISSING,
        )
        result = self.session.run_ps(runner)
        if result.status_code == REMOTE_SCRIPT_MISSING:
            self._upload_script()
            result = self.session.run_ps(runner)
        if result.status_code != 0:
            error = result.std_err.decode("utf-8", "replace").strip()
            raise RuntimeError(error[:500] or "powershell exited %d" % result.status_code)
        return json.loads(result.std_out.decode("utf-8-sig", "replace"))


class ScvmmCollector:
    """Serves the last snapshot; never talks to SCVMM itself."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = None
        self._up = 0.0
        self._duration = 0.0
        self._auth_failed = 0.0

    def update(self, data, duration):
        with self._lock:
            self._data = data
            self._up = 1.0 if data is not None else 0.0
            self._duration = duration

    def auth_failed(self):
        with self._lock:
            self._auth_failed = 1.0

    def collect(self):
        with self._lock:
            data, up, duration = self._data, self._up, self._duration
            auth_failed = self._auth_failed

        yield GaugeMetricFamily(
            "scvmm_up", "1 if the last SCVMM query succeeded.", value=up
        )
        yield GaugeMetricFamily(
            "scvmm_scrape_duration_seconds",
            "Duration of the last SCVMM query.",
            value=duration,
        )
        yield GaugeMetricFamily(
            "scvmm_auth_failed",
            "1 if VMM rejected the credential and querying has stopped.",
            value=auth_failed,
        )
        if data is None:
            return

        for metric in self._vm_metrics(as_list(data.get("vms"))):
            yield metric
        for metric in self._host_metrics(as_list(data.get("vmhosts"))):
            yield metric
        for metric in self._cluster_metrics(as_list(data.get("clusters"))):
            yield metric

        jobs = GaugeMetricFamily(
            "scvmm_jobs",
            "Recent VMM jobs by status (last 200).",
            labels=["status"],
        )
        for status, count in sorted((data.get("jobs") or {}).items()):
            jobs.add_metric([text(status)], num(count))
        yield jobs

    @staticmethod
    def _vm_metrics(vms):
        info = GaugeMetricFamily(
            "scvmm_vm_info",
            "Virtual machine metadata; the value is always 1.",
            labels=["vm", "vmhost", "cloud", "os", "status"],
        )
        cpu = GaugeMetricFamily(
            "scvmm_vm_cpu_count", "Virtual CPUs assigned.", labels=["vm"]
        )
        memory = GaugeMetricFamily(
            "scvmm_vm_memory_bytes", "Startup memory assigned.", labels=["vm"]
        )
        storage = GaugeMetricFamily(
            "scvmm_vm_storage_bytes", "Total virtual disk size.", labels=["vm"]
        )
        running = GaugeMetricFamily(
            "scvmm_vm_running", "1 if the VM is running.", labels=["vm"]
        )
        total = GaugeMetricFamily(
            "scvmm_vms", "Virtual machines by status.", labels=["status"]
        )

        counts = {}
        for vm in vms:
            name = text(vm.get("name"))
            status = text(vm.get("status"))
            info.add_metric(
                [
                    name,
                    text(vm.get("host")),
                    text(vm.get("cloud")),
                    text(vm.get("os")),
                    status,
                ],
                1.0,
            )
            cpu.add_metric([name], num(vm.get("cpu")))
            # VMM reports VM memory in MiB and host memory in bytes.
            memory.add_metric([name], num(vm.get("memory_mb")) * MB)
            storage.add_metric([name], num(vm.get("storage_bytes")))
            running.add_metric([name], 1.0 if status.lower() == "running" else 0.0)
            counts[status] = counts.get(status, 0) + 1

        for status, count in sorted(counts.items()):
            total.add_metric([status], float(count))
        return [info, cpu, memory, storage, running, total]

    @staticmethod
    def _host_metrics(vmhosts):
        info = GaugeMetricFamily(
            "scvmm_host_info",
            "Hyper-V host metadata; the value is always 1.",
            labels=["vmhost", "cluster", "state", "hypervisor"],
        )
        up = GaugeMetricFamily(
            "scvmm_host_up", "1 if the host is responding to VMM.", labels=["vmhost"]
        )
        health = GaugeMetricFamily(
            "scvmm_host_healthy",
            "1 if the host's VMM overall state is OK.",
            labels=["vmhost"],
        )
        cpu = GaugeMetricFamily(
            "scvmm_host_cpu_utilization_percent",
            "Host CPU utilisation.",
            labels=["vmhost"],
        )
        memory_total = GaugeMetricFamily(
            "scvmm_host_memory_total_bytes", "Host physical memory.", labels=["vmhost"]
        )
        memory_available = GaugeMetricFamily(
            "scvmm_host_memory_available_bytes",
            "Host memory available for placement.",
            labels=["vmhost"],
        )
        capacity = GaugeMetricFamily(
            "scvmm_host_storage_capacity_bytes",
            "Host disk volume capacity.",
            labels=["vmhost"],
        )
        free = GaugeMetricFamily(
            "scvmm_host_storage_free_bytes",
            "Host disk volume free space.",
            labels=["vmhost"],
        )
        vms = GaugeMetricFamily(
            "scvmm_host_vms", "Virtual machines placed on the host.", labels=["vmhost"]
        )

        for vmhost in vmhosts:
            name = text(vmhost.get("name"))
            info.add_metric(
                [
                    name,
                    text(vmhost.get("cluster")),
                    text(vmhost.get("state")),
                    text(vmhost.get("hypervisor")),
                ],
                1.0,
            )
            up.add_metric([name], healthy(vmhost.get("computer_state")))
            health.add_metric([name], healthy(vmhost.get("state")))
            cpu.add_metric([name], num(vmhost.get("cpu_percent")))
            memory_total.add_metric([name], num(vmhost.get("memory_total_bytes")))
            # AvailableMemory is MiB even though TotalMemory is bytes.
            memory_available.add_metric(
                [name], num(vmhost.get("memory_available_mb")) * MB
            )
            capacity.add_metric([name], num(vmhost.get("storage_capacity_bytes")))
            free.add_metric([name], num(vmhost.get("storage_free_bytes")))
            vms.add_metric([name], num(vmhost.get("vm_count")))

        return [
            info,
            up,
            health,
            cpu,
            memory_total,
            memory_available,
            capacity,
            free,
            vms,
        ]

    @staticmethod
    def _cluster_metrics(clusters):
        info = GaugeMetricFamily(
            "scvmm_cluster_info",
            "Host cluster metadata; the value is always 1.",
            labels=["cluster", "state", "validation_state", "quorum", "virtualization"],
        )
        health = GaugeMetricFamily(
            "scvmm_cluster_healthy",
            "1 if the cluster state is nominal.",
            labels=["cluster"],
        )
        nodes = GaugeMetricFamily(
            "scvmm_cluster_nodes", "Nodes in the cluster.", labels=["cluster"]
        )
        nodes_up = GaugeMetricFamily(
            "scvmm_cluster_nodes_up",
            "Cluster nodes responding to VMM.",
            labels=["cluster"],
        )
        node_up = GaugeMetricFamily(
            "scvmm_cluster_node_up",
            "1 if the cluster node is up.",
            labels=["cluster", "node", "state"],
        )
        volume_capacity = GaugeMetricFamily(
            "scvmm_cluster_volume_capacity_bytes",
            "Cluster shared volume capacity.",
            labels=["cluster", "volume"],
        )
        volume_free = GaugeMetricFamily(
            "scvmm_cluster_volume_free_bytes",
            "Cluster shared volume free space.",
            labels=["cluster", "volume"],
        )
        volume_available = GaugeMetricFamily(
            "scvmm_cluster_volume_available",
            "1 if the shared volume is available for placement.",
            labels=["cluster", "volume"],
        )

        for cluster in clusters:
            name = text(cluster.get("name"))
            state = text(cluster.get("state")) or text(cluster.get("overall_state"))
            info.add_metric(
                [
                    name,
                    state,
                    text(cluster.get("validation_state")),
                    text(cluster.get("quorum")),
                    text(cluster.get("virtualization")),
                ],
                1.0,
            )
            health.add_metric([name], healthy(state))

            cluster_nodes = as_list(cluster.get("nodes"))
            nodes.add_metric([name], float(len(cluster_nodes)))
            up_count = 0
            for node in cluster_nodes:
                # ClusterNodeStatus is the membership answer; ComputerState is
                # the reachability one. A node VMM cannot reach is down here
                # whatever the cluster service last recorded about it.
                node_state = text(node.get("cluster_state")) or text(node.get("state"))
                value = min(
                    healthy(node.get("computer_state")),
                    healthy(node_state) if node_state else 1.0,
                )
                node_up.add_metric([name, text(node.get("name")), node_state], value)
                up_count += int(value)
            nodes_up.add_metric([name], float(up_count))

            for volume in as_list(cluster.get("volumes")):
                label = text(volume.get("name"))
                volume_capacity.add_metric([name, label], num(volume.get("capacity")))
                volume_free.add_metric([name, label], num(volume.get("free")))
                volume_available.add_metric(
                    [name, label], 1.0 if volume.get("accessible") else 0.0
                )

        return [
            info,
            health,
            nodes,
            nodes_up,
            node_up,
            volume_capacity,
            volume_free,
            volume_available,
        ]


def main():
    logging.basicConfig(
        format="%(levelname)s %(message)s",
        level=env("SCVMM_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
    )

    host = env("SCVMM_HOST", required=True)
    port = int(env("SCVMM_WINRM_PORT", "5985"))
    user = env("SCVMM_USER", required=True)
    password = env("SCVMM_PASSWORD", required=True)
    interval = float(env("SCVMM_INTERVAL", "60"))
    timeout = int(env("SCVMM_TIMEOUT", "120"))
    listen_port = int(env("SCVMM_EXPORTER_PORT", "9620"))

    registry = CollectorRegistry()
    collector = ScvmmCollector()
    registry.register(collector)

    scvmm = Scvmm(host, port, user, password, timeout)
    start_http_server(listen_port, registry=registry)
    logging.info("serving :%d, querying %s every %.0fs", listen_port, host, interval)

    while True:
        started = time.monotonic()
        try:
            data = scvmm.query()
            elapsed = time.monotonic() - started
            collector.update(data, elapsed)
            logging.info(
                "ok in %.1fs: %d vms, %d hosts, %d clusters",
                elapsed,
                len(as_list(data.get("vms"))),
                len(as_list(data.get("vmhosts"))),
                len(as_list(data.get("clusters"))),
            )
        except InvalidCredentialsError as error:
            # The one failure this loop does not retry. VMM is reached with a
            # domain account, and a domain account under a lockout policy is
            # locked out by exactly this shape of retry: a poll loop replaying
            # a rejected password every interval until the directory gives up
            # on it. That would take the account down across every machine
            # that trusts the domain, not just this exporter. So the process
            # stays up serving scvmm_auth_failed 1 -- an alertable state that
            # costs nothing -- and a corrected credential is picked up by a
            # restart.
            collector.update(None, time.monotonic() - started)
            collector.auth_failed()
            logging.error(
                "VMM rejected the credential (%s); not retrying, because this "
                "account locks out. Fix the secret and restart the unit.",
                error,
            )
            break
        except Exception as error:  # noqa: BLE001 - any failure is scvmm_up 0
            elapsed = time.monotonic() - started
            collector.update(None, elapsed)
            logging.warning("query failed after %.1fs: %s", elapsed, error)
        time.sleep(max(1.0, interval - (time.monotonic() - started)))

    # Reached only after an authentication failure: keep serving the metric
    # that says so rather than exiting into a restart loop that would retry
    # the rejected password.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
