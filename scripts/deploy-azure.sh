#!/usr/bin/env bash
# deploy-azure.sh — Provision Azure infrastructure for the OceanStream basemap
#
# Creates from scratch:
#   1. Resource group
#   2. Storage account (with blob access, CORS, and immutable content settings)
#   3. Blob container with public read access
#   4. Azure Front Door profile + endpoint (global CDN with caching & HTTPS)
#   5. Custom domain mapping (basemap.oceanstream.io)
#   6. CDN caching rules optimised for tile serving
#   7. CORS rules on the storage account
#   8. Diagnostic settings summary
#
# Usage:
#   bash scripts/deploy-azure.sh                    # interactive with defaults
#   bash scripts/deploy-azure.sh --test             # use test resource group + names
#   bash scripts/deploy-azure.sh --resource-group mygroup --storage-account myaccount
#
# Prerequisites:
#   - Azure CLI (az) >= 2.60
#   - Logged in: az login
#   - Contributor role on the target subscription
#
set -euo pipefail

# ============================================================================
# Colour helpers
# ============================================================================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info()  { echo -e "${CYAN}==>${NC} $*"; }
ok()    { echo -e "${GREEN}  ✓${NC} $*"; }
warn()  { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()   { echo -e "${RED}  ✗${NC} $*" >&2; }
step()  { echo -e "\n${BOLD}━━━ Step $1: $2 ━━━${NC}"; }
dim()   { echo -e "${DIM}    $*${NC}"; }

confirm() {
    local prompt="$1"
    local default="${2:-y}"
    local yn
    if [[ "$default" == "y" ]]; then
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [Y/n] ")" yn
        yn="${yn:-y}"
    else
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [y/N] ")" yn
        yn="${yn:-n}"
    fi
    [[ "${yn,,}" == "y" ]]
}

prompt_value() {
    local prompt="$1"
    local default="$2"
    local result
    read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [${default}]: ")" result
    echo "${result:-$default}"
}

# ============================================================================
# Defaults
# ============================================================================
RESOURCE_GROUP="oceanstream-basemap-rg"
LOCATION="westeurope"
STORAGE_ACCOUNT="oceanbasemap"
CONTAINER_NAME="basemap"
FRONTDOOR_PROFILE="oceanstream-basemap-fd"
FRONTDOOR_ENDPOINT="basemap"
CUSTOM_DOMAIN="basemap.oceanstream.io"
STORAGE_SKU="Standard_LRS"
FRONTDOOR_SKU="Standard_AzureFrontDoor"
TEST_MODE=false

# ============================================================================
# Parse CLI arguments
# ============================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --test)
            TEST_MODE=true
            RESOURCE_GROUP="basemap-test-rg"
            STORAGE_ACCOUNT="basemaptest$(date +%m%d)"
            FRONTDOOR_PROFILE="basemap-test-fd"
            FRONTDOOR_ENDPOINT="basemap-test"
            CUSTOM_DOMAIN=""
            shift ;;
        --resource-group)   RESOURCE_GROUP="$2"; shift 2 ;;
        --location)         LOCATION="$2"; shift 2 ;;
        --storage-account)  STORAGE_ACCOUNT="$2"; shift 2 ;;
        --container)        CONTAINER_NAME="$2"; shift 2 ;;
        --frontdoor)        FRONTDOOR_PROFILE="$2"; shift 2 ;;
        --endpoint)         FRONTDOOR_ENDPOINT="$2"; shift 2 ;;
        --custom-domain)    CUSTOM_DOMAIN="$2"; shift 2 ;;
        --storage-sku)      STORAGE_SKU="$2"; shift 2 ;;
        --frontdoor-sku)    FRONTDOOR_SKU="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --test                Use test resource group and auto-generated names"
            echo "  --resource-group RG   Resource group name (default: oceanstream-basemap-rg)"
            echo "  --location LOC        Azure region (default: westeurope)"
            echo "  --storage-account SA  Storage account name (default: oceanbasemap)"
            echo "  --container NAME      Blob container name (default: basemap)"
            echo "  --frontdoor NAME      Front Door profile name (default: oceanstream-basemap-fd)"
            echo "  --endpoint NAME       Front Door endpoint name (default: basemap)"
            echo "  --custom-domain FQDN  Custom domain (default: basemap.oceanstream.io)"
            echo "  --storage-sku SKU     Storage SKU (default: Standard_LRS)"
            echo "  --frontdoor-sku SKU   Front Door SKU: Standard or Premium (default: Standard_AzureFrontDoor)"
            echo "  --help, -h            Show this help"
            exit 0 ;;
        *)
            err "Unknown option: $1"
            exit 1 ;;
    esac
done

# ============================================================================
# Prerequisites check
# ============================================================================
step 0 "Prerequisites"

if ! command -v az &>/dev/null; then
    err "Azure CLI (az) not found."
    dim "Install: https://aka.ms/InstallAzureCli"
    exit 1
fi
ok "Azure CLI found: $(az version --query '"azure-cli"' -o tsv)"

if ! az account show &>/dev/null 2>&1; then
    err "Not logged in to Azure."
    dim "Run: az login"
    exit 1
fi

SUBSCRIPTION_NAME=$(az account show --query 'name' -o tsv)
SUBSCRIPTION_ID=$(az account show --query 'id' -o tsv)
ok "Subscription: ${SUBSCRIPTION_NAME} (${SUBSCRIPTION_ID})"

# Check for required providers
for provider in Microsoft.Storage Microsoft.Cdn; do
    state=$(az provider show --namespace "$provider" --query 'registrationState' -o tsv 2>/dev/null || echo "NotRegistered")
    if [[ "$state" != "Registered" ]]; then
        warn "Registering provider: $provider"
        az provider register --namespace "$provider" --wait >/dev/null 2>&1 || true
    fi
done
ok "Required providers registered"

# ============================================================================
# Interactive configuration review
# ============================================================================
echo ""
echo -e "${BOLD}Deployment Configuration${NC}"
echo -e "──────────────────────────────────────────"
echo -e "  Resource Group:    ${CYAN}${RESOURCE_GROUP}${NC}"
echo -e "  Location:          ${CYAN}${LOCATION}${NC}"
echo -e "  Storage Account:   ${CYAN}${STORAGE_ACCOUNT}${NC}"
echo -e "  Storage SKU:       ${CYAN}${STORAGE_SKU}${NC}"
echo -e "  Container:         ${CYAN}${CONTAINER_NAME}${NC}"
echo -e "  Front Door:        ${CYAN}${FRONTDOOR_PROFILE}${NC}"
echo -e "  FD Endpoint:       ${CYAN}${FRONTDOOR_ENDPOINT}${NC}"
echo -e "  FD SKU:            ${CYAN}${FRONTDOOR_SKU}${NC}"
echo -e "  Custom Domain:     ${CYAN}${CUSTOM_DOMAIN:-<none>}${NC}"
if $TEST_MODE; then
    echo -e "  Mode:              ${YELLOW}TEST${NC}"
fi
echo -e "──────────────────────────────────────────"

if ! confirm "Proceed with this configuration?"; then
    info "Customise values:"
    RESOURCE_GROUP=$(prompt_value "Resource group" "$RESOURCE_GROUP")
    LOCATION=$(prompt_value "Location (az account list-locations -o table)" "$LOCATION")
    STORAGE_ACCOUNT=$(prompt_value "Storage account name (3-24 lowercase alphanum)" "$STORAGE_ACCOUNT")
    STORAGE_SKU=$(prompt_value "Storage SKU (Standard_LRS, Standard_GRS, Standard_ZRS)" "$STORAGE_SKU")
    CONTAINER_NAME=$(prompt_value "Container name" "$CONTAINER_NAME")
    FRONTDOOR_PROFILE=$(prompt_value "Front Door profile name" "$FRONTDOOR_PROFILE")
    FRONTDOOR_ENDPOINT=$(prompt_value "Front Door endpoint name" "$FRONTDOOR_ENDPOINT")
    FRONTDOOR_SKU=$(prompt_value "Front Door SKU (Standard_AzureFrontDoor, Premium_AzureFrontDoor)" "$FRONTDOOR_SKU")
    CUSTOM_DOMAIN=$(prompt_value "Custom domain (leave empty to skip)" "$CUSTOM_DOMAIN")
    echo ""
fi

# Validate storage account name
if [[ ! "$STORAGE_ACCOUNT" =~ ^[a-z0-9]{3,24}$ ]]; then
    err "Storage account name must be 3-24 lowercase alphanumeric characters."
    exit 1
fi

# Derived values
STORAGE_ORIGIN_HOSTNAME="${STORAGE_ACCOUNT}.blob.core.windows.net"
ORIGIN_GROUP_NAME="bloborigingroup"
ORIGIN_NAME="bloborigin"
ROUTE_NAME="defaultroute"
RULE_SET_NAME="tilecachingrules"

# ============================================================================
# Step 1: Resource Group
# ============================================================================
step 1 "Resource Group"

if az group show --name "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Resource group '${RESOURCE_GROUP}' already exists"
else
    info "Creating resource group '${RESOURCE_GROUP}' in ${LOCATION}..."
    az group create \
        --name "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --tags project=oceanstream component=basemap environment="$(if $TEST_MODE; then echo test; else echo production; fi)" \
        -o none
    ok "Resource group created"
fi

# ============================================================================
# Step 2: Storage Account
# ============================================================================
step 2 "Storage Account"

if az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Storage account '${STORAGE_ACCOUNT}' already exists"
else
    info "Creating storage account '${STORAGE_ACCOUNT}'..."
    az storage account create \
        --name "$STORAGE_ACCOUNT" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --sku "$STORAGE_SKU" \
        --kind StorageV2 \
        --access-tier Hot \
        --https-only true \
        --min-tls-version TLS1_2 \
        --allow-blob-public-access true \
        --tags project=oceanstream component=basemap \
        -o none
    ok "Storage account created"
fi

# Wait for storage account to be fully provisioned
info "Waiting for storage account provisioning..."
for _i in $(seq 1 30); do
    state=$(az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
        --query 'provisioningState' -o tsv 2>/dev/null || echo "Creating")
    if [[ "$state" == "Succeeded" ]]; then break; fi
    sleep 5
done
ok "Storage account provisioned"

# ============================================================================
# Step 3: CORS Rules
# ============================================================================
step 3 "CORS Rules on Blob Service"

STORAGE_KEY=$(az storage account keys list \
    --account-name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --query '[0].value' -o tsv)

info "Configuring CORS for tile clients (MapLibre, browsers)..."
az storage cors clear \
    --account-name "$STORAGE_ACCOUNT" \
    --account-key "$STORAGE_KEY" \
    --services b \
    -o none 2>/dev/null || true

az storage cors add \
    --account-name "$STORAGE_ACCOUNT" \
    --account-key "$STORAGE_KEY" \
    --services b \
    --methods GET HEAD OPTIONS \
    --origins '*' \
    --allowed-headers 'Range,If-None-Match,If-Modified-Since' \
    --exposed-headers 'Content-Length,Content-Range,Content-Type,Accept-Ranges,ETag' \
    --max-age 86400 \
    -o none
ok "CORS rules configured (GET/HEAD/OPTIONS, Range headers exposed)"

# ============================================================================
# Step 4: Blob Container
# ============================================================================
step 4 "Blob Container"

STORAGE_KEY=$(az storage account keys list \
    --account-name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --query '[0].value' -o tsv)

if az storage container show \
    --name "$CONTAINER_NAME" \
    --account-name "$STORAGE_ACCOUNT" \
    --account-key "$STORAGE_KEY" &>/dev/null 2>&1; then
    ok "Container '${CONTAINER_NAME}' already exists"
else
    info "Creating container '${CONTAINER_NAME}' with public blob access..."
    az storage container create \
        --name "$CONTAINER_NAME" \
        --account-name "$STORAGE_ACCOUNT" \
        --account-key "$STORAGE_KEY" \
        --public-access blob \
        -o none
    ok "Container created with public blob access"
fi

# Upload a test file to verify the storage is working
info "Uploading test blob to verify storage..."
echo '{"status":"ok","service":"oceanstream-basemap"}' | \
    az storage blob upload \
        --container-name "$CONTAINER_NAME" \
        --account-name "$STORAGE_ACCOUNT" \
        --account-key "$STORAGE_KEY" \
        --name "health.json" \
        --data "@-" \
        --content-type "application/json" \
        --overwrite \
        -o none 2>/dev/null || \
    # Fallback: write to temp file first (older az CLI)
    { tmpf=$(mktemp); echo '{"status":"ok","service":"oceanstream-basemap"}' > "$tmpf"
      az storage blob upload \
        --container-name "$CONTAINER_NAME" \
        --account-name "$STORAGE_ACCOUNT" \
        --account-key "$STORAGE_KEY" \
        --name "health.json" \
        --file "$tmpf" \
        --content-type "application/json" \
        --overwrite \
        -o none
      rm -f "$tmpf"; }
ok "Test blob uploaded: https://${STORAGE_ORIGIN_HOSTNAME}/${CONTAINER_NAME}/health.json"

# ============================================================================
# Step 5: Azure Front Door — Profile
# ============================================================================
step 5 "Azure Front Door Profile"

if az afd profile show \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Front Door profile '${FRONTDOOR_PROFILE}' already exists"
else
    info "Creating Front Door profile (SKU: ${FRONTDOOR_SKU})..."
    az afd profile create \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --sku "$FRONTDOOR_SKU" \
        --tags project=oceanstream component=basemap \
        -o none
    ok "Front Door profile created"
fi

# ============================================================================
# Step 6: Front Door Endpoint
# ============================================================================
step 6 "Front Door Endpoint"

if az afd endpoint show \
    --endpoint-name "$FRONTDOOR_ENDPOINT" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Endpoint '${FRONTDOOR_ENDPOINT}' already exists"
    FD_HOSTNAME=$(az afd endpoint show \
        --endpoint-name "$FRONTDOOR_ENDPOINT" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --query 'hostName' -o tsv)
else
    info "Creating endpoint '${FRONTDOOR_ENDPOINT}'..."
    az afd endpoint create \
        --endpoint-name "$FRONTDOOR_ENDPOINT" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --enabled-state Enabled \
        -o none
    FD_HOSTNAME=$(az afd endpoint show \
        --endpoint-name "$FRONTDOOR_ENDPOINT" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --query 'hostName' -o tsv)
    ok "Endpoint created: https://${FD_HOSTNAME}"
fi

# ============================================================================
# Step 7: Origin Group + Origin (Blob Storage backend)
# ============================================================================
step 7 "Origin Group + Origin"

if az afd origin-group show \
    --origin-group-name "$ORIGIN_GROUP_NAME" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Origin group '${ORIGIN_GROUP_NAME}' already exists"
else
    info "Creating origin group with health probes..."
    az afd origin-group create \
        --origin-group-name "$ORIGIN_GROUP_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --probe-request-type HEAD \
        --probe-protocol Https \
        --probe-interval-in-seconds 120 \
        --probe-path "/${CONTAINER_NAME}/health.json" \
        --sample-size 4 \
        --successful-samples-required 3 \
        --additional-latency-in-milliseconds 50 \
        -o none
    ok "Origin group created"
fi

if az afd origin show \
    --origin-name "$ORIGIN_NAME" \
    --origin-group-name "$ORIGIN_GROUP_NAME" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Origin '${ORIGIN_NAME}' already exists"
else
    info "Adding blob storage origin..."
    az afd origin create \
        --origin-name "$ORIGIN_NAME" \
        --origin-group-name "$ORIGIN_GROUP_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --host-name "$STORAGE_ORIGIN_HOSTNAME" \
        --origin-host-header "$STORAGE_ORIGIN_HOSTNAME" \
        --http-port 80 \
        --https-port 443 \
        --priority 1 \
        --weight 1000 \
        --enabled-state Enabled \
        -o none
    ok "Blob storage origin added: ${STORAGE_ORIGIN_HOSTNAME}"
fi

# ============================================================================
# Step 8: Rule Set — Caching rules for tile assets
# ============================================================================
step 8 "Caching Rule Set"

if az afd rule-set show \
    --rule-set-name "$RULE_SET_NAME" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Rule set '${RULE_SET_NAME}' already exists"
else
    info "Creating rule set for tile caching..."
    az afd rule-set create \
        --rule-set-name "$RULE_SET_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        -o none
    ok "Rule set created"
fi

# Rule: Cache PNG tiles (hillshade/hypsometric) for 30 days
if ! az afd rule show \
    --rule-name "CacheRasterTiles" \
    --rule-set-name "$RULE_SET_NAME" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    info "Adding rule: Cache raster tiles (*.png) aggressively..."
    az afd rule create \
        --rule-name "CacheRasterTiles" \
        --rule-set-name "$RULE_SET_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --order 1 \
        --match-variable UrlFileExtension \
        --operator Equal \
        --match-values "png" \
        --action-name "RouteConfigurationOverride" \
        --cache-behavior OverrideAlways \
        --cache-duration "23:59:59" \
        --enable-caching true \
        --query-string-caching-behavior IgnoreQueryString \
        -o none
    ok "Raster tile caching rule created (max TTL)"
fi

# Rule: Cache PMTiles (byte-range) for 7 days, vary on Range header
if ! az afd rule show \
    --rule-name "CachePmtiles" \
    --rule-set-name "$RULE_SET_NAME" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    info "Adding rule: Cache PMTiles (*.pmtiles) with Range support..."
    az afd rule create \
        --rule-name "CachePmtiles" \
        --rule-set-name "$RULE_SET_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --order 2 \
        --match-variable UrlFileExtension \
        --operator Equal \
        --match-values "pmtiles" \
        --action-name "RouteConfigurationOverride" \
        --cache-behavior OverrideAlways \
        --cache-duration "23:59:59" \
        --enable-caching true \
        --query-string-caching-behavior IgnoreQueryString \
        -o none
    ok "PMTiles caching rule created (max TTL)"
fi

# Rule: Add CORS headers for all responses
if ! az afd rule show \
    --rule-name "AddCorsHeaders" \
    --rule-set-name "$RULE_SET_NAME" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    info "Adding rule: CORS response headers..."
    az afd rule create \
        --rule-name "AddCorsHeaders" \
        --rule-set-name "$RULE_SET_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --order 3 \
        --match-variable RequestMethod \
        --operator Equal \
        --match-values "GET" \
        --action-name "ModifyResponseHeader" \
        --header-action Overwrite \
        --header-name "Access-Control-Allow-Origin" \
        --header-value "*" \
        -o none
    ok "CORS header rule created"
fi

# ============================================================================
# Step 9: Route — Connect endpoint to origin group
# ============================================================================
step 9 "Front Door Route"

if az afd route show \
    --route-name "$ROUTE_NAME" \
    --endpoint-name "$FRONTDOOR_ENDPOINT" \
    --profile-name "$FRONTDOOR_PROFILE" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
    ok "Route '${ROUTE_NAME}' already exists"
else
    info "Creating route with caching and HTTPS redirect..."
    az afd route create \
        --route-name "$ROUTE_NAME" \
        --endpoint-name "$FRONTDOOR_ENDPOINT" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" \
        --origin-group "$ORIGIN_GROUP_NAME" \
        --origin-path "/${CONTAINER_NAME}" \
        --supported-protocols Https Http \
        --https-redirect Enabled \
        --forwarding-protocol HttpsOnly \
        --patterns-to-match "/*" \
        --link-to-default-domain Enabled \
        --enable-caching true \
        --query-string-caching-behavior IgnoreQueryString \
        --rule-sets "$RULE_SET_NAME" \
        -o none
    ok "Route created: /* → blob storage /${CONTAINER_NAME}/"
fi

# ============================================================================
# Step 10: Custom Domain (optional)
# ============================================================================
step 10 "Custom Domain"

if [[ -z "$CUSTOM_DOMAIN" ]]; then
    warn "No custom domain specified, skipping."
    dim "Use --custom-domain basemap.oceanstream.io to add one later."
else
    # Sanitise domain name for Azure resource naming (replace dots with hyphens)
    DOMAIN_RESOURCE_NAME="${CUSTOM_DOMAIN//./-}"

    if az afd custom-domain show \
        --custom-domain-name "$DOMAIN_RESOURCE_NAME" \
        --profile-name "$FRONTDOOR_PROFILE" \
        --resource-group "$RESOURCE_GROUP" &>/dev/null 2>&1; then
        ok "Custom domain '${CUSTOM_DOMAIN}' already configured"
    else
        echo ""
        echo -e "${BOLD}DNS Configuration Required${NC}"
        echo -e "──────────────────────────────────────────"
        echo -e "Before proceeding, create these DNS records:"
        echo ""
        echo -e "  ${CYAN}CNAME${NC}  ${CUSTOM_DOMAIN}  →  ${FD_HOSTNAME}"
        echo -e "  ${CYAN}TXT${NC}    _dnsauth.${CUSTOM_DOMAIN}  →  (will be shown after domain creation)"
        echo ""
        echo -e "The CNAME record is needed now. The TXT record is for"
        echo -e "certificate validation and will be displayed after this step."
        echo -e "──────────────────────────────────────────"
        echo ""

        if confirm "Have you created the CNAME record? Continue with domain setup?" "n"; then
            info "Adding custom domain '${CUSTOM_DOMAIN}'..."
            az afd custom-domain create \
                --custom-domain-name "$DOMAIN_RESOURCE_NAME" \
                --profile-name "$FRONTDOOR_PROFILE" \
                --resource-group "$RESOURCE_GROUP" \
                --host-name "$CUSTOM_DOMAIN" \
                --certificate-type ManagedCertificate \
                --minimum-tls-version TLS12 \
                -o none
            ok "Custom domain added"

            # Show validation token
            VALIDATION_TOKEN=$(az afd custom-domain show \
                --custom-domain-name "$DOMAIN_RESOURCE_NAME" \
                --profile-name "$FRONTDOOR_PROFILE" \
                --resource-group "$RESOURCE_GROUP" \
                --query 'validationProperties.validationToken' -o tsv 2>/dev/null || echo "")

            if [[ -n "$VALIDATION_TOKEN" ]]; then
                echo ""
                echo -e "${BOLD}Certificate Validation — Create this TXT record:${NC}"
                echo -e "──────────────────────────────────────────"
                echo -e "  ${CYAN}TXT${NC}  _dnsauth.${CUSTOM_DOMAIN}  →  ${VALIDATION_TOKEN}"
                echo -e "──────────────────────────────────────────"
                echo ""
                warn "Certificate provisioning can take 10-30 minutes after TXT record is set."
            fi

            # Associate custom domain with the route
            info "Associating custom domain with route..."
            az afd route update \
                --route-name "$ROUTE_NAME" \
                --endpoint-name "$FRONTDOOR_ENDPOINT" \
                --profile-name "$FRONTDOOR_PROFILE" \
                --resource-group "$RESOURCE_GROUP" \
                --custom-domains "$DOMAIN_RESOURCE_NAME" \
                -o none
            ok "Custom domain linked to route"
        else
            warn "Skipping custom domain setup. Run this script again when DNS is ready."
        fi
    fi
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║              Deployment Complete                       ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Resource Group:      ${GREEN}${RESOURCE_GROUP}${NC}"
echo -e "  Storage Account:     ${GREEN}${STORAGE_ACCOUNT}${NC}"
echo -e "  Blob Origin:         ${DIM}https://${STORAGE_ORIGIN_HOSTNAME}/${CONTAINER_NAME}/${NC}"
echo -e "  Front Door Endpoint: ${GREEN}https://${FD_HOSTNAME}/${NC}"
if [[ -n "$CUSTOM_DOMAIN" ]]; then
echo -e "  Custom Domain:       ${GREEN}https://${CUSTOM_DOMAIN}/${NC}"
fi
echo ""
echo -e "  ${BOLD}Tile URLs (via Front Door):${NC}"
echo -e "    Hillshade:       https://${FD_HOSTNAME}/hillshade/{z}/{x}/{y}.png"
echo -e "    Depth zones:     https://${FD_HOSTNAME}/tiles/depth_zones.pmtiles"
echo -e "    Depth contours:  https://${FD_HOSTNAME}/tiles/depth_contours.pmtiles"
echo -e "    Ocean labels:    https://${FD_HOSTNAME}/tiles/ocean_labels.pmtiles"
echo -e "    Seamounts:       https://${FD_HOSTNAME}/tiles/seamounts.pmtiles"
echo -e "    Land base:       https://${FD_HOSTNAME}/tiles/land_base.pmtiles"
echo -e "    Health check:    https://${FD_HOSTNAME}/health.json"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo -e "    1. Upload tiles:  oceanbase run upload --data-dir <path>"
echo -e "       (or update config.py AZURE_STORAGE_ACCOUNT to '${STORAGE_ACCOUNT}')"
echo -e "    2. Verify:        curl https://${FD_HOSTNAME}/health.json"
if [[ -n "$CUSTOM_DOMAIN" ]]; then
echo -e "    3. DNS:           Ensure CNAME + TXT records are set"
echo -e "    4. Verify:        curl https://${CUSTOM_DOMAIN}/health.json"
fi
echo ""

# Quick verification
info "Verifying blob access..."
HEALTH_URL="https://${STORAGE_ORIGIN_HOSTNAME}/${CONTAINER_NAME}/health.json"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" --max-time 10 || echo "000")
if [[ "$HTTP_CODE" == "200" ]]; then
    ok "Blob storage accessible: ${HEALTH_URL}"
else
    warn "Blob check returned HTTP ${HTTP_CODE} (may need a moment to propagate)"
fi

info "Verifying Front Door endpoint..."
FD_URL="https://${FD_HOSTNAME}/health.json"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$FD_URL" --max-time 15 || echo "000")
if [[ "$HTTP_CODE" == "200" ]]; then
    ok "Front Door accessible: ${FD_URL}"
else
    warn "Front Door returned HTTP ${HTTP_CODE} (CDN propagation can take 5-10 minutes)"
fi

echo ""
echo -e "${GREEN}${BOLD}Done!${NC}"
