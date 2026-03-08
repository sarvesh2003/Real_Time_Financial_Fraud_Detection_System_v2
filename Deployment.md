# Deployment Guide — Real-Time Fraud Detection System (GCP)

This guide documents the exact steps taken to deploy the fraud detection system to **Google Cloud Platform (GCP)** using a **GCE VM + Docker Compose** approach with pre-built Docker images.

---

## Prerequisites

- Windows machine with PowerShell
- A Google Cloud account with billing enabled
- Pre-built Docker images already pushed to Docker Hub:
  - `sarvesh3006/fraud-detection-server:latest`
  - `sarvesh3006/fraud-detection-producer:latest`
  - `sarvesh3006/fraud-detection-enricher:latest`
- MaxMind GeoIP credentials (Account ID + License Key)
- A `.env` file in the project root with the following content:
  ```
  GEOIPUPDATE_ACCOUNT_ID=<your_account_id>
  GEOIPUPDATE_LICENSE_KEY=<your_license_key>
  GEOIPUPDATE_EDITION_IDS=GeoLite2-City GeoLite2-ASN
  GEOIPUPDATE_FREQUENCY=24
  ```

---

## Step 1 — Install Google Cloud SDK on Windows

Download and silently install the Google Cloud SDK:

```powershell
Invoke-WebRequest -Uri "https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe" -OutFile "$env:TEMP\GoogleCloudSDKInstaller.exe"

Start-Process -FilePath "$env:TEMP\GoogleCloudSDKInstaller.exe" -ArgumentList "/S", "/allusers" -Wait
```

After installation, refresh the PATH in your current PowerShell session so `gcloud` is available without restarting:

```powershell
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
```

Verify the installation:

```powershell
gcloud version
```

Expected output: `Google Cloud SDK 559.0.0` (or newer).

> **Note:** On Windows, always use backtick `` ` `` for line continuation in PowerShell, not backslash `\`. All multi-line commands below use this convention.

---

## Step 2 — Authenticate with Google Cloud

Open a browser-based login flow:

```powershell
gcloud auth login
```

This opens your default browser. Log in with your Google account. On success you will see:
```
You are now logged in as [your-email@gmail.com].
```

---

## Step 3 — Select GCP Project

List your available projects:

```powershell
gcloud projects list
```

Set the target project (replace `YOUR_PROJECT_ID` with the actual project ID from the list):

```powershell
gcloud config set project YOUR_PROJECT_ID
```

In this deployment, the project used was `<YOUR_PROJECT_ID>` (Gemini Project).

---

## Step 4 — Enable the Compute Engine API

```powershell
gcloud services enable compute.googleapis.com
```

Wait for the operation to complete. Expected output:
```
Operation "operations/..." finished successfully.
```

---

## Step 5 — Create the GCE Virtual Machine

```powershell
gcloud compute instances create fraud-detection-vm `
  --machine-type=e2-standard-4 `
  --image-family=debian-12 `
  --image-project=debian-cloud `
  --boot-disk-size=50GB `
  --zone=us-central1-a `
  --tags=fraud-detection
```

**VM Specs:**
| Property | Value |
|---|---|
| Machine type | e2-standard-4 (4 vCPU, 16 GB RAM) |
| OS | Debian 12 (Bookworm) |
| Disk | 50 GB |
| Zone | us-central1-a |
| Network tag | fraud-detection |

On success you will see the VM's `INTERNAL_IP` and `EXTERNAL_IP`. Note the **External IP** — this is the public address of your deployment.

---

## Step 6 — Open Firewall Port 50051 (gRPC)

```powershell
gcloud compute firewall-rules create allow-grpc --allow=tcp:50051 --target-tags=fraud-detection --description="Allow gRPC traffic for fraud detection"
```

This allows inbound TCP traffic on port `50051` to any VM tagged with `fraud-detection`.

---

## Step 7 — Install Docker on the VM

Run Docker installation remotely over SSH in a single command:

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="curl -fsSL https://get.docker.com | sh ; sudo usermod -aG docker $USER ; sudo apt-get install -y docker-compose-plugin"
```

This does three things:
1. Downloads and runs the official Docker install script
2. Adds the current user to the `docker` group (avoids needing `sudo` for docker commands — takes effect on next login)
3. Installs the Docker Compose plugin

Verify Docker is installed and running:

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="docker version"
```

---

## Step 8 — Copy Files to the VM

Copy the `docker-compose.public.yml` and `.env` files from your local machine to the VM:

```powershell
gcloud compute scp "C:\Users\<your-username>\OneDrive\Documents\Fraud_Detection_System\docker-compose.public.yml" "fraud-detection-vm:/home/<your-username>/docker-compose.public.yml" --zone=us-central1-a

gcloud compute scp "C:\Users\<your-username>\OneDrive\Documents\Fraud_Detection_System\.env" "fraud-detection-vm:/home/<your-username>/.env" --zone=us-central1-a
```

> **Important:** Replace `<your-username>` with your actual Windows username. The destination path on the VM must use your GCP username (same as your Google account username prefix, e.g., `sarve`). **Do not use `~/` in the destination** — it does not work with the Windows `pscp` client used by gcloud.

Verify files are present on the VM:

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="ls -la /home/<your-username>/"
```

---

## Step 9 — Start the Stack

Start all services in detached mode using docker compose with the env file:

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="cd /home/<your-username> ; sudo docker compose -f docker-compose.public.yml --env-file .env up -d"
```

Docker will pull all images from Docker Hub and start the following containers:

| Container | Image | Role |
|---|---|---|
| `sarve-zookeeper-1` | `confluentinc/cp-zookeeper:7.4.0` | Kafka coordination |
| `sarve-kafka-1` | `confluentinc/cp-kafka:7.4.0` | Message streaming |
| `sarve-redis1-1` | `redis:latest` | Redis cluster node 1 |
| `sarve-redis2-1` | `redis:latest` | Redis cluster node 2 |
| `sarve-redis3-1` | `redis:latest` | Redis cluster node 3 |
| `sarve-redis-cluster-setup-1` | `redis:latest` | One-shot cluster init (exits after done) |
| `sarve-geoip-updater-1` | `maxmindinc/geoipupdate` | Downloads MaxMind GeoLite2 databases |
| `sarve-go-server-1` | `sarvesh3006/fraud-detection-server:latest` | gRPC ingestion server |
| `sarve-go-enricher-1` | `sarvesh3006/fraud-detection-enricher:latest` | Kafka stream enricher |
| `sarve-python-producer-1` | `sarvesh3006/fraud-detection-producer:latest` | Mesa agent transaction generator |

> The stack startup takes ~90 seconds. The Redis cluster setup container runs once, initializes the 3-node cluster, and exits — this is expected.

---

## Step 10 — Verify the Deployment

### Check container status

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="sudo docker compose -f /home/<your-username>/docker-compose.public.yml ps"
```

All containers should show `Up` status. `sarve-redis-cluster-setup-1` will show `Exited (0)` which is expected.

### Check gRPC server logs

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="sudo docker logs sarve-go-server-1 --tail=20"
```

Expected output:
```
Go gRPC Server listening at [::]:50051
Received & Pushed: User=... | Amt=...
```

### Check enricher logs

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a --command="sudo docker logs sarve-go-enricher-1 --tail=20"
```

Expected output:
```
ENRICHED_TXN ip=... city=... country=... isp=... txn_count_2h=... total_2h=...
Published Enriched transaction to enriched_transactions
HIGH VELOCITY ALERT: IP ... spending .../hour!
```

### Check Kafka topics

SSH into the VM first, then:

```bash
sudo docker exec sarve-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list
```

Expected topics: `raw_transactions`, `enriched_transactions`

### Monitor enriched transactions live

```bash
sudo docker exec sarve-kafka-1 kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic enriched_transactions \
  --from-beginning \
  --max-messages 5
```

---

## Interacting with the Deployment

### SSH into the VM

```powershell
gcloud compute ssh fraud-detection-vm --zone=us-central1-a
```

### Useful docker commands (run inside the VM)

```bash
# Live follow logs of any service
sudo docker logs sarve-go-server-1 -f
sudo docker logs sarve-go-enricher-1 -f
sudo docker logs sarve-python-producer-1 -f
sudo docker logs sarve-kafka-1 --tail=50

# Check all container statuses
sudo docker compose -f /home/sarve/docker-compose.public.yml ps

# Stop the stack
sudo docker compose -f /home/sarve/docker-compose.public.yml down

# Restart the stack
sudo docker compose -f /home/sarve/docker-compose.public.yml --env-file .env up -d

# Check Redis cluster health
sudo docker exec sarve-redis1-1 redis-cli -p 6379 cluster info
```

---

## Architecture on GCP

```
Internet
    ↓ TCP:50051
GCE VM (e2-standard-4) — External IP: <External IP>
    └── Docker network: redis-cluster-net (192.168.240.0/24)
            ├── python-producer   → gRPC → go-server:50051
            ├── go-server         → Kafka → raw_transactions
            ├── go-enricher       → Kafka → enriched_transactions
            ├── kafka + zookeeper
            ├── redis1 (192.168.240.100)
            ├── redis2 (192.168.240.101)
            ├── redis3 (192.168.240.102)
            └── geoip-updater     → /data/geoip volume
```

---

## Cost Estimate

| Resource | Type | Estimated Cost |
|---|---|---|
| GCE VM | e2-standard-4, us-central1-a | ~$0.13/hr (~$97/month) |
| Persistent Disk | 50 GB SSD | ~$4.25/month |
| Egress | Minimal for this workload | ~$0 |

> **To avoid ongoing charges**, stop the VM when not in use:
> ```powershell
> gcloud compute instances stop fraud-detection-vm --zone=us-central1-a
> ```
> Restart it later:
> ```powershell
> gcloud compute instances start fraud-detection-vm --zone=us-central1-a
> ```
> Note: The external IP may change on restart unless you reserve a static IP.

---

## Troubleshooting

### `gcloud` not recognized in PowerShell
Refresh PATH in the current session:
```powershell
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
```

### `pscp: unable to open ~/path` error on `gcloud compute scp`
Use the full absolute path instead of `~/`:
```powershell
# Wrong
"fraud-detection-vm:~/file.yml"

# Correct
"fraud-detection-vm:/home/<your-username>/file.yml"
```

### Redis cluster `CLUSTERDOWN` error on enricher startup
The enricher has `restart: on-failure` set — it will automatically retry until the Redis cluster setup container finishes and the cluster reaches `OK` state. Wait ~30–60 seconds and check logs again.

### Go enricher not producing enriched transactions
Check that `geoip-updater` has finished downloading the databases:
```bash
sudo docker logs sarve-geoip-updater-1
sudo docker exec sarve-go-enricher-1 ls /data/geoip/
```
The enricher volume mounts `/data/geoip` read-only and requires `GeoLite2-City.mmdb` and `GeoLite2-ASN.mmdb` to be present.

### Line continuation errors in PowerShell
PowerShell uses backtick `` ` `` not backslash `\` for line continuation. Always use:
```powershell
gcloud compute instances create fraud-detection-vm `
  --machine-type=e2-standard-4 `
  --zone=us-central1-a
```
