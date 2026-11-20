from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException
import urllib3
import functools

# Suppress warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Proxmox Tech Support API")
@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    """Serves the main HTML dashboard"""
    with open("index.html", "r") as f:
        return f.read()
# ==========================================
# CONFIGURATION
# ==========================================
PROXMOX_HOST = "10.34.7.200"
PROXMOX_USER = "root@pam"
PROXMOX_TOKEN_NAME = "portalapps"
PROXMOX_TOKEN_VALUE = "ea5ee17d-3082-45cb-a873-4bbba54e81a5"

proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_USER,
    token_name=PROXMOX_TOKEN_NAME,
    token_value=PROXMOX_TOKEN_VALUE,
    verify_ssl=False
)

# ==========================================
# PYDANTIC MODELS
# ==========================================
class PowerAction(BaseModel):
    action: str  # 'start', 'stop', or 'shutdown'

class SnapshotCreate(BaseModel):
    snapname: str

class PoolChange(BaseModel):
    new_pool: str

# ==========================================
# HELPER FUNCTIONS
# ==========================================
# @functools.lru_cache caches the result so we don't query Proxmox repeatedly
@functools.lru_cache(maxsize=128)
def get_vm_node(vmid: int):
    """Finds the host node for a given VM."""
    try:
        resources = proxmox.cluster.resources.get(type='vm')
        for vm in resources:
            if vm.get("vmid") == vmid:
                return vm.get("node")
        raise HTTPException(status_code=403, detail="VM not found or access denied")
    except ResourceException as e:
        raise HTTPException(status_code=500, detail=f"Proxmox API Error: {str(e)}")

# ==========================================
# API ENDPOINTS
# ==========================================
@app.get("/api/pools")
def get_available_pools():
    """Returns a list of all Technical Support pools for the dropdown menu"""
    try:
        pools = proxmox.pools.get()
        valid_pools = []
        for pool in pools:
            raw_id = pool.get("poolid", "")
            if "Technical Support" not in raw_id and "TechnicalSupport" not in raw_id:
                continue
            
            display_pool = raw_id
            for prefix in ["TechnicalSupport/", "Technical Support/", "TechnicalSupport-", "Technical Support-"]:
                if display_pool.startswith(prefix):
                    display_pool = display_pool[len(prefix):]
            
            if display_pool in ["Technical Support", "TechnicalSupport"]:
                display_pool = "Master Pool"
            
            display_pool = display_pool.replace("-", " ").strip()
            valid_pools.append({"raw_pool": raw_id, "display_name": display_pool})
            
        return {"pools": valid_pools}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/api/vms/{vmid}/ip")
def get_vm_ip(vmid: int):
    """Fetches the IP address for a specific VM using the QEMU Guest Agent"""
    node = get_vm_node(vmid)
    try:
        # 1. Check if VM is running (Agent only works if running)
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        if status.get("status") != "running":
            return {"ip": "Offline"}

        # 2. Query the guest agent
        interfaces = proxmox.nodes(node).qemu(vmid).agent.get("network-get-interfaces")
        
        # 3. Find the first valid IPv4 address that isn't localhost
        for iface in interfaces.get("result", []):
            for ip in iface.get("ip-addresses", []):
                if ip.get("ip-address-type") == "ipv4" and ip.get("ip-address") != "127.0.0.1":
                    return {"ip": ip.get("ip-address")}
                    
        return {"ip": "No IP found"}
    except ResourceException as e:
        # Usually means the QEMU guest agent is not enabled in Proxmox or installed in the VM
        return {"ip": "Agent disabled"}
    except Exception as e:
        return {"ip": "Error"}
@app.get("/api/vms")
def list_vms():
    """Fetches all VMs and their pools in a SINGLE API call"""
    try:
        resources = proxmox.cluster.resources.get(type="vm")
        vms = []
        for vm in resources:
            raw_pool = vm.get("pool", "")
            
            # Filter out templates and other departments
            if "Technical Support" not in raw_pool and "TechnicalSupport" not in raw_pool:
                continue
            
            # Clean up the nested pool name for the UI
            display_pool = raw_pool
            for prefix in ["TechnicalSupport/", "Technical Support/", "TechnicalSupport-", "Technical Support-"]:
                if display_pool.startswith(prefix):
                    display_pool = display_pool[len(prefix):]
            
            if display_pool in ["Technical Support", "TechnicalSupport"]:
                display_pool = "Master Pool"
            
            display_pool = display_pool.replace("-", " ").strip()
            
            vms.append({
                "vmid": vm.get("vmid"),
                "name": vm.get("name", "Unknown"),
                "status": vm.get("status", "unknown"),
                "node": vm.get("node"),
                "pool": display_pool,
                "raw_pool": raw_pool # Helpful for the frontend when changing pools
            })
        return {"vms": vms}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/vms/{vmid}/pool")
def change_vm_pool(vmid: int, request: PoolChange):
    """Moves a VM to a new pool"""
    try:
        # Proxmox automatically removes it from the old pool when added to a new one
        proxmox.pools(request.new_pool).put(vms=vmid)
        return {"message": f"VM {vmid} successfully moved to pool '{request.new_pool}'"}
    except ResourceException as e:
        raise HTTPException(status_code=500, detail=f"Failed to change pool: {str(e)}")

@app.post("/api/vms/{vmid}/power")
def power_manage_vm(vmid: int, request: PowerAction):
    """Starts, stops, or gracefully shuts down a VM"""
    node = get_vm_node(vmid)
    try:
        vm_api = proxmox.nodes(node).qemu(vmid).status
        if request.action == "start":
            vm_api.start.post()
        elif request.action == "stop":
            vm_api.stop.post()
        elif request.action == "shutdown":
            vm_api.shutdown.post()
        else:
            raise HTTPException(status_code=400, detail="Invalid power action")
        
        return {"message": f"Power action '{request.action}' initiated for VM {vmid}"}
    except ResourceException as e:
        # If a VM migrated, our cached node might be wrong. Clear cache and fail gracefully.
        get_vm_node.cache_clear()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/vms/{vmid}/snapshots")
def list_snapshots(vmid: int):
    node = get_vm_node(vmid)
    try:
        snaps = proxmox.nodes(node).qemu(vmid).snapshot.get()
        valid_snaps = [s for s in snaps if s.get('name') != 'current']
        return {"snapshots": valid_snaps}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vmid}/snapshots")
def create_snapshot(vmid: int, request: SnapshotCreate):
    """Creates a new snapshot with RAM state included"""
    node = get_vm_node(vmid)
    try:
        proxmox.nodes(node).qemu(vmid).snapshot.post(
            snapname=request.snapname, 
            description="Created via Tech Support Portal",
            vmstate=1 
        )
        return {"message": f"Snapshot '{request.snapname}' creation started"}
    except ResourceException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vmid}/snapshots/{snapname}/rollback")
def rollback_snapshot(vmid: int, snapname: str):
    """Reverts a VM to a specified snapshot"""
    node = get_vm_node(vmid)
    try:
        proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post()
        return {"message": f"VM {vmid} rolled back to '{snapname}'"}
    except ResourceException as e:
        raise HTTPException(status_code=500, detail=str(e))
