# scvmm-exporter

A small Prometheus exporter for System Center Virtual Machine Manager, with a
Grafana dashboard. One file, two dependencies, no agent on the VMM server.

It runs a single PowerShell query over WinRM against the VMM management server
on a fixed interval and serves the last snapshot on `/metrics`. Scrapes never
block on SCVMM: a slow or unreachable VMM server is `scvmm_up 0`, not a scrape
timeout. A failed query drops the snapshot rather than serving it, so no VM,
host or cluster gauge outlives the fleet state it described.

The VMM cmdlets are the only supported read path — the SQL schema is not a
contract — and each one is expensive enough that asking per metric would cost
more than the whole scrape budget. So it asks once, for everything.

## Metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `scvmm_up` | | 1 if the last query succeeded |
| `scvmm_auth_failed` | | 1 if VMM rejected the credential and querying stopped |
| `scvmm_scrape_duration_seconds` | | duration of the last query |
| `scvmm_cluster_info` | cluster, state, validation_state, quorum, virtualization | cluster metadata |
| `scvmm_cluster_healthy` | cluster | 1 if the cluster state is nominal |
| `scvmm_cluster_nodes` | cluster | nodes in the cluster |
| `scvmm_cluster_nodes_up` | cluster | nodes VMM can reach |
| `scvmm_cluster_node_up` | cluster, node, state | per-node membership and reachability |
| `scvmm_cluster_volume_capacity_bytes` | cluster, volume | CSV capacity |
| `scvmm_cluster_volume_free_bytes` | cluster, volume | CSV free space |
| `scvmm_cluster_volume_available` | cluster, volume | 1 if available for placement |
| `scvmm_host_info` | vmhost, cluster, state, hypervisor | host metadata |
| `scvmm_host_up` | vmhost | 1 if the host is responding to VMM |
| `scvmm_host_healthy` | vmhost | 1 if the host's VMM overall state is OK |
| `scvmm_host_cpu_utilization_percent` | vmhost | host CPU |
| `scvmm_host_memory_total_bytes` | vmhost | physical memory |
| `scvmm_host_memory_available_bytes` | vmhost | memory available for placement |
| `scvmm_host_storage_capacity_bytes` | vmhost | disk volume capacity |
| `scvmm_host_storage_free_bytes` | vmhost | disk volume free space |
| `scvmm_host_vms` | vmhost | VMs placed on the host |
| `scvmm_vm_info` | vm, vmhost, cloud, os, status | VM metadata |
| `scvmm_vm_running` | vm | 1 if the VM is running |
| `scvmm_vm_cpu_count` | vm | virtual CPUs assigned |
| `scvmm_vm_memory_bytes` | vm | startup memory assigned |
| `scvmm_vm_storage_bytes` | vm | total virtual disk size |
| `scvmm_vms` | status | VM count by status |
| `scvmm_jobs` | status | recent VMM jobs by status (last 200) |

The host label is `vmhost`, not `host`, so it survives a scrape config that
attaches its own `host` label to the target without being rewritten to
`exported_host`.

VMM reports VM memory in MiB and host memory in bytes, and host available
memory in MiB while host total memory is in bytes. The exporter normalises all
of it to bytes.

## Configuration

Everything is an environment variable. Only the first three are required.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCVMM_HOST` | | VMM management server address |
| `SCVMM_USER` | | e.g. `DOMAIN\Administrator` |
| `SCVMM_PASSWORD` | | password for that account |
| `SCVMM_WINRM_PORT` | `5985` | WinRM port |
| `SCVMM_EXPORTER_PORT` | `9620` | listen port |
| `SCVMM_INTERVAL` | `60` | seconds between queries |
| `SCVMM_TIMEOUT` | `120` | WinRM operation timeout |
| `SCVMM_LOG_LEVEL` | `INFO` | Python log level |

## Running it

Requires Python 3, [pywinrm](https://pypi.org/project/pywinrm/) and
[prometheus-client](https://pypi.org/project/prometheus-client/). The VMM
server needs WinRM enabled and the `virtualmachinemanager` PowerShell module
installed, which the VMM console installs.

```sh
pip install pywinrm prometheus-client

SCVMM_HOST=vmm01.example.com \
SCVMM_USER='EXAMPLE\svc-prometheus' \
SCVMM_PASSWORD=... \
python scvmm_exporter.py
```

Then scrape it:

```yaml
scrape_configs:
  - job_name: scvmm
    scrape_interval: 60s
    static_configs:
      - targets: ["vmm-exporter-host:9620"]
```

Scraping faster than `SCVMM_INTERVAL` only reprints the same numbers.

`examples/scvmm-exporter.service` is a hardened systemd unit that reads the
password from a credential file rather than the environment.

## A rejected credential is not retried

VMM is reached with a domain account, and a domain account under a lockout
policy is locked out by exactly the shape of retry a poll loop performs:
replaying a rejected password every interval until the directory gives up on
it. That takes the account down across every machine that trusts the domain,
not just this exporter.

So on a 401 the exporter stops querying, permanently. It stays up serving
`scvmm_auth_failed 1` — an alertable state that costs nothing — and picks up a
corrected credential on restart. If you see `AUTH REJECTED` on the dashboard,
fix the stored credential and restart the service; do not retry the password by
hand.

A read-only VMM role for the exporter's account is a good idea. Nothing here
issues a write of any kind, but the account is only as constrained as you make
it.

## Dashboard

`dashboards/scvmm.json` — cluster health first (per-node up/down, CSV usage,
cluster state), then hosts, then VM inventory and job status. Import it and
pick your Prometheus datasource.

Unknown SCVMM properties degrade to `0`/`""` rather than failing the query, so
a build of VMM that does not carry one of them shows a zero instead of taking
the exporter down with it.
