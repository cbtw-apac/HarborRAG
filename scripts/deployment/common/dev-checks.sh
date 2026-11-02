# Preconditions for scripts/deployment/dev.sh. Sourced, never executed.
# The sourcing script sets ROOT_DIR; every check exits with status 2 when it fails.

fail() {
    echo "$1" >&2
    exit 2
}

require_file() {
    local path="$1"
    local label="$2"
    [[ -f "${ROOT_DIR}/${path}" ]] || fail "Missing ${label}: ${path}. Run '$0 bootstrap'."
}

require_positive_integer() {
    local name="$1"
    local value="$2"
    [[ "${value}" =~ ^[1-9][0-9]*$ ]] || fail "${name} must be a positive integer."
}

validate_device() {
    [[ "$1" == "cpu" || "$1" == "gpu" ]] || fail "--device must be cpu or gpu, got: $1"
}

require_data_network() {
    docker network inspect harborrag-data-network >/dev/null 2>&1 ||
        fail "harborrag-data-network is not running. Start it with '$0 data'."
}
