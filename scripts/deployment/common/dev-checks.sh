# Preconditions and env-file helpers for scripts/deployment/dev.sh and
# scripts/deployment/observability.sh. Sourced, never executed.
# The sourcing script sets ROOT_DIR; every check exits with status 2 when it fails.
# Env files are read with sed, never sourced, so their values are not run as shell code.

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
        fail "harborrag-data-network is not running. Start it with 'scripts/deployment/dev.sh data'."
}

# GNU, BSD and BusyBox sed all accept an attached backup suffix; plain `sed -i` is GNU-only.
replace_env_line() {
    sed -i.bak "$1" "$2"
    rm -f "$2.bak"
}

# The last NAME=value of an env file, read the way Compose reads it: without a carriage
# return, a trailing comment or surrounding quotes. Empty when unset.
env_file_value() {
    local value
    value="$(sed -n "s/^$2=//p" "${ROOT_DIR}/$1" | tail -n 1)"
    value="${value%$'\r'}"
    value="${value%%[[:space:]]#*}"
    value="${value#[\"\']}"
    value="${value%[\"\']}"
    printf '%s\n' "${value}"
}

# Write a random value for NAME into an env file when it has none.
ensure_generated_value() {
    local file="$1" name="$2"
    local target_path="${ROOT_DIR}/${file}"
    local value

    [[ -z "$(env_file_value "${file}" "${name}")" ]] || return 0
    command -v openssl >/dev/null || fail "OpenSSL is required to generate ${name}."
    value="$(openssl rand -hex 32)"
    if grep -Eq "^${name}=" "${target_path}"; then
        replace_env_line "s/^${name}=.*/${name}=${value}/" "${target_path}"
    else
        printf '\n%s=%s\n' "${name}" "${value}" >>"${target_path}"
    fi
    chmod 600 "${target_path}"
    echo "Generated ${name} in ${file}."
}

# Create an env file from its template with mode 0600, or protect the existing one.
# Sets created_environment=1 when it created the file.
ensure_environment_file() {
    local target="$1"
    local template="$2"

    if [[ -f "${ROOT_DIR}/${target}" ]]; then
        chmod 600 "${ROOT_DIR}/${target}"
        return
    fi
    umask 077
    mkdir -p "$(dirname "${ROOT_DIR}/${target}")"
    cp "${ROOT_DIR}/${template}" "${ROOT_DIR}/${target}"
    chmod 600 "${ROOT_DIR}/${target}"
    echo "Created ${target} from ${template}."
    created_environment=1
}
